"""What a television client reads: what is live, what was recorded, and the
video itself. The Apple TV app is the first reader, and the web page is the
second.

Everything here is JSON under /api/tv, and the viewing copies under /hls.
"""
import hashlib
import os
import re
import secrets
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .. import auth, db, handover, passes, recorder, tuner
from . import passes as pass_routes
from . import record
from ._shared import VERSION, current_user, page

router = APIRouter()

_SEGMENT = re.compile(r"^seg\d{5}\.ts$")
_UID = re.compile(r"^[0-9a-f]{32}$")
# A pairing code nobody approved is forgotten after this long.
PAIR_SECONDS = 600


# ---------- who is asking ----------

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def device_for(request: Request):
    """The paired device this request carries a key for, or None."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    row = db.one("SELECT * FROM tv_devices WHERE key_hash = ? AND approved_at IS NOT NULL",
                 (_hash(header[7:].strip()),))
    if row and (row["last_seen"] or 0) < int(time.time()) - 300:
        with db.tx() as c:
            c.execute("UPDATE tv_devices SET last_seen = ? WHERE id = ?",
                      (int(time.time()), row["id"]))
    return row


@router.get("/api/tv/hello")
def hello(request: Request):
    """The first thing a client asks. Open, so it can find out whether it has
    to pair before anything else will answer."""
    needs = auth.mode() != "none"
    dev = device_for(request)
    # `paired` is whether this television has a key, with sign-in on or off.
    # Every television gets one, because its own settings hang off it. With
    # sign-in off the key is handed over without anybody approving a code.
    return {"ok": True, "name": "CouchElephant", "version": VERSION,
            "pairing_required": needs,
            "paired": bool(dev),
            "can_delete": bool(dev and dev["allow_delete"]),
            "recorder": recorder.why_unavailable() is None,
            "recorder_note": recorder.why_unavailable()}


@router.post("/api/tv/pair")
def pair_start(name: str = Form("Apple TV")):
    """A client asks to be let in. It shows the code; a person types the code
    into Settings. The key is handed over only to whoever holds the secret."""
    now = int(time.time())
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
    secret = secrets.token_urlsafe(24)
    with db.tx() as c:
        c.execute("DELETE FROM tv_devices WHERE approved_at IS NULL AND created_at < ?",
                  (now - PAIR_SECONDS,))
        # With sign-in off there is nobody to approve it and nothing to guard,
        # so it is let in as it asks.
        c.execute("INSERT INTO tv_devices (name, code, poll_hash, approved_at, created_at) "
                  "VALUES (?, ?, ?, ?, ?)",
                  (name[:60], code, _hash(secret),
                   now if auth.mode() == "none" else None, now))
    return {"ok": True, "code": code, "secret": secret, "expires_in": PAIR_SECONDS}


@router.post("/api/tv/pair/poll")
def pair_poll(secret: str = Form(...)):
    """Has somebody approved the code yet. Answers the key exactly once."""
    row = db.one("SELECT * FROM tv_devices WHERE poll_hash = ?", (_hash(secret),))
    if not row:
        return JSONResponse({"ok": False, "error": "That code has expired. Start again."},
                            status_code=404)
    if not row["approved_at"]:
        return {"ok": True, "approved": False}
    key = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET key_hash = ?, poll_hash = NULL, code = NULL "
                  "WHERE id = ?", (_hash(key), row["id"]))
    return {"ok": True, "approved": True, "key": key}


# ---------- letting a device in, from Settings ----------

@router.get("/api/tv/devices")
def devices():
    now = int(time.time())
    rows = db.query(
        "SELECT id, name, code, approved_at, created_at, last_seen, allow_delete "
        "FROM tv_devices "
        "WHERE approved_at IS NOT NULL OR created_at > ? ORDER BY created_at DESC",
        (now - PAIR_SECONDS,))
    return {"ok": True, "devices": [dict(r) for r in rows]}


@router.post("/api/tv/devices/approve")
def device_approve(code: str = Form(...)):
    code = code.strip().upper()
    row = db.one("SELECT * FROM tv_devices WHERE code = ? AND approved_at IS NULL "
                 "AND created_at > ?", (code, int(time.time()) - PAIR_SECONDS))
    if not row:
        return JSONResponse({"ok": False, "error":
                             "No television is showing that code. Check it and try again."},
                            status_code=404)
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET approved_at = ? WHERE id = ?",
                  (int(time.time()), row["id"]))
    return {"ok": True, "name": row["name"]}


@router.post("/api/tv/devices/{device_id}/settings")
def device_settings(device_id: int, allow_delete: str = Form("0")):
    """What one television may do. Only delete, so far."""
    if not db.one("SELECT 1 FROM tv_devices WHERE id = ?", (device_id,)):
        return JSONResponse({"ok": False, "error": "That television is gone."},
                            status_code=404)
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET allow_delete = ? WHERE id = ?",
                  (1 if allow_delete in ("1", "true", "on") else 0, device_id))
    return {"ok": True}


@router.post("/api/tv/devices/{device_id}/delete")
def device_delete(device_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM tv_devices WHERE id = ?", (device_id,))
    return {"ok": True}


# ---------- shaping rows for a client ----------

def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _art(program_guid):
    if not program_guid:
        return ""
    p = db.one("SELECT thumb, art FROM programs WHERE guid = ?", (program_guid,))
    return (p["art"] or p["thumb"] or "") if p else ""


def _channel(request, vcn, title):
    has_logo = db.one("SELECT 1 FROM channels WHERE vcn = ? AND logo_path IS NOT NULL "
                      "AND logo_path != ''", (vcn,))
    return {"number": vcn, "name": title or "",
            "logo": f"{_base(request)}/logo/{vcn}" if has_logo else ""}


def _names(show, title, sport=False):
    """The big line and the small line.

    A series leads with its own name and puts the episode under it, because
    "Episode 4" says nothing in a grid. Sport is the other way round: the
    game is the name, and "NFL Football" is the small print.
    """
    if show and title and show != title:
        return (title, show) if sport else (show, title)
    return title or show or "", ""


def _sport_show(show) -> bool:
    """Is this a sports programme, asked by its series name. For the rows that
    come from Plex's schedule, which do not carry the guide's section."""
    return bool(show and db.one(
        "SELECT 1 FROM programs WHERE grandparent_title = ? AND section = 'sports' "
        "LIMIT 1", (show,)))


def recording_json(request: Request, rec) -> dict:
    view = recorder.view_info(rec["uid"])
    big, small = _names(rec["show_title"], rec["title"], rec["section"] == "sports")
    state = rec["state"]
    return {
        "id": rec["id"],
        "kind": rec["kind"],
        "state": state,
        "recorded_by": "couchelephant",
        "title": big, "subtitle": small,
        "channel": _channel(request, rec["channel_vcn"], rec["channel_title"]),
        "art": _art(rec["program_guid"]),
        "begins_at": rec["begins_at"], "ends_at": rec["ends_at"],
        "started_at": rec["started_at"], "ended_at": rec["ended_at"],
        # How much there is to watch right now, which for a recording still
        # being made is less than how long it has been running.
        "seconds": round(view["seconds"], 1),
        "complete": view["complete"],
        "position": round(rec["position"] or 0, 1),
        "playable": view["segments"] > 0 or state in ("in_plex",),
        "playlist": f"{_base(request)}/hls/{rec['uid']}/index.m3u8",
        "error": rec["error"] if state == "failed" else None,
        # What a list of recordings sorts and filters by.
        "group": rec["show_title"] or rec["title"] or "",
        "category": _category(rec),
        "season": rec["season"], "episode": rec["episode"],
        "airing_id": rec["airing_id"],
    }


def _category(rec) -> str:
    """'movies', 'sports' or 'tv', which is all a filter needs to ask."""
    if rec["ptype"] == "movie":
        return "movies"
    return "sports" if rec["section"] == "sports" else "tv"


def _may_delete(request: Request) -> bool:
    """A television may delete only if its own setting says so. A person in
    the web app may if they could change that setting themselves."""
    dev = device_for(request)
    if dev:
        return bool(dev["allow_delete"])
    if auth.mode() == "none":
        return True
    user = current_user(request)
    return bool(user and user.get("role") == "admin")


def _plex_now(request, now):
    """What Plex is recording at this moment. Listed, never playable."""
    out = []
    for g in db.query(
            "SELECT * FROM plex_grabs WHERE begins_at <= ? AND ends_at > ? "
            "ORDER BY begins_at", (now, now)):
        ours = db.one("SELECT 1 FROM recordings WHERE channel_vcn = ? AND begins_at = ? "
                      "AND kind = 'recording'", (g["channel_vcn"], g["begins_at"]))
        if ours:
            continue
        ch = db.one("SELECT title FROM channels WHERE vcn = ?", (g["channel_vcn"],))
        big, small = _names(g["parent_title"], g["title"], _sport_show(g["parent_title"]))
        out.append({
            "id": f"plex:{g['id']}", "kind": "recording", "state": "recording",
            "recorded_by": "plex", "title": big, "subtitle": small,
            "channel": _channel(request, g["channel_vcn"], ch["title"] if ch else ""),
            "art": "", "begins_at": g["begins_at"], "ends_at": g["ends_at"],
            "seconds": max(0, now - (g["begins_at"] or now)), "complete": False,
            "position": 0, "playable": False, "playlist": "",
            "note": "Cannot play while Plex is recording",
        })
    return out


_refused: dict = {"at": 0, "vcns": set()}


def _unwatchable() -> set:
    """Channels the tuner cannot give us, asked at most every ten minutes."""
    now = time.time()
    if now - _refused["at"] > 600:
        try:
            _refused["vcns"] = {str(c.get("GuideNumber")) for c in tuner.lineup()
                                if tuner.refusal(c)}
        except tuner.TunerError:
            pass
        _refused["at"] = now
    return _refused["vcns"]


def _vcn_key(vcn):
    try:
        return float(vcn)
    except (TypeError, ValueError):
        return 1e9


def _on_now(request, now):
    skip = _unwatchable()
    rows = db.query(
        """SELECT a.channel_vcn, a.channel_title, a.begins_at, a.ends_at,
                  p.guid, p.title, p.grandparent_title, p.thumb, p.art, p.section
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at <= ? AND a.ends_at > ? AND COALESCE(a.drm, 0) = 0""",
        (now, now))
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: _vcn_key(r["channel_vcn"])):
        vcn = r["channel_vcn"]
        if vcn in seen or vcn in skip:
            continue
        seen.add(vcn)
        big, small = _names(r["grandparent_title"], r["title"], r["section"] == "sports")
        out.append({"channel": _channel(request, vcn, r["channel_title"]),
                    "title": big, "subtitle": small,
                    "art": r["art"] or r["thumb"] or "",
                    "begins_at": r["begins_at"], "ends_at": r["ends_at"]})
    return out


# ---------- the web page ----------

@router.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    """The same three lists the television app opens on, in a browser."""
    return page(request, "live.html")


# ---------- the lists ----------

@router.get("/api/tv/home")
def home(request: Request):
    now = int(time.time())
    live = [recording_json(request, r) for r in db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' AND state = 'recording' "
        "ORDER BY begins_at")]
    recent = [recording_json(request, r) for r in db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('finished', 'moving', 'in_plex', 'failed') "
        "ORDER BY begins_at DESC LIMIT 40")]
    note = recorder.why_unavailable()
    return {"ok": True, "now": now,
            "recorder": note is None, "recorder_note": note,
            "can_delete": _may_delete(request),
            "live_now": live + _plex_now(request, now),
            "recent": recent,
            "on_now": _on_now(request, now) if note is None else []}


@router.get("/api/tv/recordings/{rec_id}")
def recording(request: Request, rec_id: int):
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    # A recording whose copy was tidied away gets a new one made on demand.
    if rec["state"] in ("finished", "moving", "in_plex"):
        recorder.ensure_view(rec, rec["plex_path"] if rec["state"] == "in_plex" else None)
    return {"ok": True, "recording": recording_json(request, rec)}


@router.post("/api/tv/recordings/{rec_id}/position")
def position(rec_id: int, seconds: float = Form(...)):
    """Where the player is. Also how the app knows somebody is watching, which
    is what holds a finished recording back from being moved to Plex."""
    if not recorder.get(rec_id):
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    recorder.watching(rec_id, seconds)
    return {"ok": True}


# ---------- the guide, for a television ----------

GUIDE_HOURS = 24


def _booked_slots(now):
    """Every broadcast set to record or recording, by slot, and who has it."""
    out = {}
    for r in db.query("SELECT channel_vcn, begins_at FROM plex_grabs "
                      "WHERE status IN ('scheduled', 'inprogress') AND ends_at > ?", (now,)):
        out[(r["channel_vcn"], r["begins_at"])] = "plex"
    for r in db.query("SELECT channel_vcn, begins_at FROM recordings WHERE kind = 'recording' "
                      "AND state IN ('scheduled', 'recording')"):
        out[(r["channel_vcn"], r["begins_at"])] = "couchelephant"
    return out


@router.get("/api/tv/guide")
def tv_guide(request: Request, hours: int = GUIDE_HOURS):
    """The grid: every channel, and what is on it from now for a day.

    One answer, not pages. A day of guide for a household's channels is a few
    hundred kilobytes, and a television that has it all can scroll without
    waiting.
    """
    now = int(time.time())
    until = now + max(1, min(48, hours)) * 3600
    skip = _unwatchable()
    booked = _booked_slots(now)
    chans = db.query("SELECT vcn, call_sign, title, logo_path FROM channels")
    rows = db.query(
        """SELECT a.id, a.channel_vcn, a.begins_at, a.ends_at, a.premiere,
                  COALESCE(a.drm, 0) AS drm, p.title, p.grandparent_title, p.section
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.ends_at > ? AND a.begins_at < ?
           ORDER BY a.begins_at""", (now, until))
    by = {}
    for r in rows:
        by.setdefault(r["channel_vcn"], []).append(r)
    out = []
    for c in sorted(chans, key=lambda c: _vcn_key(c["vcn"])):
        mine, last_end = [], 0
        for r in by.get(c["vcn"], []):
            if r["begins_at"] < last_end:
                continue        # the guide sometimes lists one slot twice
            last_end = r["ends_at"]
            big, small = _names(r["grandparent_title"], r["title"], r["section"] == "sports")
            mine.append({
                "id": r["id"], "title": big, "subtitle": small,
                "begins_at": r["begins_at"], "ends_at": r["ends_at"],
                "live": bool(r["premiere"]), "sport": r["section"] == "sports",
                "recording": booked.get((c["vcn"], r["begins_at"])),
                "recordable": not r["drm"] and c["vcn"] not in skip,
            })
        if mine:
            out.append({"channel": _channel(request, c["vcn"], c["title"] or c["call_sign"]),
                        "watchable": c["vcn"] not in skip, "programs": mine})
    return {"ok": True, "now": now, "start": now, "end": until, "channels": out}


@router.get("/api/tv/program")
def tv_program(request: Request, airing_id: str):
    """One broadcast, for the panel a television opens on it."""
    a = db.one(
        """SELECT a.*, p.title, p.grandparent_title, p.summary, p.teams, p.thumb,
                  p.art, p.year, p.section, p.season, p.episode, p.content_rating
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.id = ?""", (airing_id,))
    if not a:
        return JSONResponse({"ok": False, "error":
                             "That broadcast is no longer in the guide."}, status_code=404)
    now = int(time.time())
    big, small = _names(a["grandparent_title"], a["title"], a["section"] == "sports")
    mine = recorder.booked(a)
    plex_has = db.one(
        "SELECT 1 FROM plex_grabs WHERE channel_vcn = ? AND begins_at = ? "
        "AND status IN ('scheduled', 'inprogress')", (a["channel_vcn"], a["begins_at"]))
    followed = {r["team_id"] for r in db.query(
        "SELECT team_id FROM passes WHERE kind = 'team' AND enabled = 1")}
    series = a["grandparent_title"] or ""
    return {"ok": True, "program": {
        "id": a["id"], "title": big, "subtitle": small,
        "summary": a["summary"] or "", "art": a["art"] or a["thumb"] or "",
        "channel": _channel(request, a["channel_vcn"], a["channel_title"]),
        "begins_at": a["begins_at"], "ends_at": a["ends_at"],
        "on_now": a["begins_at"] <= now < a["ends_at"],
        "season": a["season"], "episode": a["episode"], "year": a["year"],
        "rating": a["content_rating"] or "",
        "recording": "couchelephant" if mine else ("plex" if plex_has else None),
        "recording_id": mine["id"] if mine else None,
        "recordable": (not a["drm"] and a["channel_vcn"] not in _unwatchable()
                       and a["ends_at"] > now),
        # What "every one of these" can mean for this programme. A game is
        # followed by its teams; anything else by its series.
        "teams": [{"id": t.get("id"), "name": t.get("name"),
                   "followed": t.get("id") in followed}
                  for t in (db.unjs(a["teams"]) or []) if t.get("id") and t.get("name")],
        "series": series if series and a["section"] != "sports" else "",
        "series_followed": bool(series and db.one(
            "SELECT 1 FROM passes WHERE kind = 'series' AND series_title = ? "
            "AND enabled = 1", (series,))),
    }}


@router.post("/api/tv/record")
def tv_record(airing_id: str = Form(...), scope: str = Form("one"),
              team_id: str = Form("")):
    """Record from a television. Always with CouchElephant's own recorder,
    because that is the only kind of recording a television here can play.

    `scope` is `one` for this broadcast, `series` for every episode, or `team`
    with a `team_id` for every game that team plays.
    """
    if db.get_setting("dry_run") == "1":
        return JSONResponse({"ok": False, "error":
                             "Preview mode is on. Turn it off in Settings to record."},
                            status_code=400)
    off = record.recorder_off()
    if off:
        return off
    row = record._airing_row(airing_id)
    if not row:
        return JSONResponse({"ok": False, "error":
                             "That broadcast is no longer in the guide."}, status_code=404)
    if scope == "one":
        if row["drm"]:
            return JSONResponse({"ok": False, "error":
                                 "This channel is encrypted and cannot be recorded."},
                                status_code=400)
        return record._record_ourselves(row, {})
    if scope == "team":
        t = db.one("SELECT * FROM teams WHERE id = ?", (team_id or 0,))
        if not t:
            return JSONResponse({"ok": False, "error": "That team is not in the guide."},
                                status_code=404)
        pass_id, label, made_it = record._make_pass(
            "team", team=dict(t), update=False, who="couchelephant")
    else:
        series = row["grandparent_title"] or row["title"]
        pass_id, label, made_it = record._make_pass(
            "series", series=series, update=False, who="couchelephant")
    if not made_it:
        # A pass that already exists keeps its source limits and its settings.
        # Only who records is changed, and it is switched back on.
        with db.tx() as c:
            c.execute("UPDATE passes SET recorder = 'couchelephant', enabled = 1 "
                      "WHERE id = ?", (pass_id,))
    done = passes.run_passes()
    made = len([d for d in done if d["action"] == "scheduled"])
    return {"ok": True, "message": f"Following {label}. {made} set to record so far."}


@router.post("/api/tv/record/cancel")
async def tv_record_cancel(airing_id: str = Form(...)):
    """Cancel one booking, whichever recorder has it."""
    return await record.api_record_cancel(airing_id=airing_id)


# ---------- everything recorded, and everything set to record ----------

@router.get("/api/tv/recordings")
def tv_recordings(request: Request):
    """Two lists: what is set to record, and what has been recorded."""
    now = int(time.time())
    upcoming = []
    for r in pass_routes._schedule_rows(start=now - 6 * 3600):
        if r["status"] not in ("scheduled", "inprogress") or (r["e"] or 0) <= now:
            continue
        ours = r.get("recorder") == "couchelephant"
        if r.get("pass_id"):
            why = r["reason"].replace("recorded by CouchElephant, for ", "")
            why = why[4:] if why.startswith("the ") else why
            kind = "pass"
        elif r["who"] == "plex" and r["reason"].startswith("a Plex rule"):
            why, kind = r["reason"].replace("a Plex rule, ", "Plex rule: "), "rule"
        else:
            why, kind = "One recording", "one"
        ch = db.one("SELECT title FROM channels WHERE vcn = ?", (r["vcn"],))
        big, small = _names(r["parent"], r["title"], _sport_show(r["parent"]))
        upcoming.append({
            "id": str(r["id"]), "airing_id": r["airing_id"] or "",
            "title": big, "subtitle": small,
            "channel": _channel(request, r["vcn"], ch["title"] if ch else ""),
            "begins_at": r["b"], "ends_at": r["e"],
            "recording_now": r["status"] == "inprogress",
            "booked_by": why[:1].upper() + why[1:], "booked_kind": kind,
            "recorder": "couchelephant" if ours else "plex",
        })
    upcoming.sort(key=lambda u: u["begins_at"] or 0)
    recorded = [recording_json(request, r) for r in db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('recording', 'finished', 'moving', 'in_plex')")]
    recorded.sort(key=lambda r: (r["group"].lower(), r["begins_at"] or 0))
    return {"ok": True, "now": now, "can_delete": _may_delete(request),
            "upcoming": upcoming, "recorded": recorded}


@router.post("/api/tv/recordings/{rec_id}/delete")
def tv_recording_delete(request: Request, rec_id: int):
    """Delete a recording for good, from here and from Plex."""
    if not _may_delete(request):
        return JSONResponse({"ok": False, "error":
                             "This television is not allowed to delete. Turn it on "
                             "under Settings, Recording, Televisions."}, status_code=403)
    rec = recorder.get(rec_id)
    if not rec or rec["kind"] != "recording":
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    try:
        handover.delete(rec)
    except handover.HandoverError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    return {"ok": True, "message": "Deleted."}


# ---------- live ----------

@router.post("/api/tv/live/{vcn}")
def live_start(request: Request, vcn: str):
    note = recorder.why_unavailable()
    if note:
        return JSONResponse({"ok": False, "error": note}, status_code=400)
    now = int(time.time())
    on = db.one(
        """SELECT p.title, p.grandparent_title, p.section, a.channel_title FROM airings a
           JOIN programs p ON p.guid = a.program_guid
           WHERE a.channel_vcn = ? AND a.begins_at <= ? AND a.ends_at > ?""",
        (vcn, now, now))
    try:
        tuner.stream_url(vcn)          # refuses an encrypted or ATSC 3.0 channel
    except tuner.TunerError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    title = (_names(on["grandparent_title"], on["title"], on["section"] == "sports")[0]
             if on else f"Channel {vcn}")
    rec_id = recorder.start_live(vcn, title, on["channel_title"] if on else None)
    return {"ok": True, "recording": recording_json(request, recorder.get(rec_id))}


@router.post("/api/tv/live/{rec_id}/keepalive")
def live_keepalive(rec_id: int):
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That session has ended."},
                            status_code=404)
    recorder.watching(rec_id)
    return {"ok": True}


# ---------- the video ----------

@router.get("/hls/{uid}/index.m3u8")
def playlist(uid: str, start: str = ""):
    if not _UID.match(uid):
        return Response(status_code=404)
    path = recorder.playlist_path(uid)
    if not os.path.exists(path):
        return Response(status_code=404)
    if start == "0":
        # A player opens a playlist that is still growing at its live end,
        # and Apple's ignores a seek made before it has loaded. This tag is
        # the playlist itself saying "start at the first second", which every
        # player obeys. Asked for by the address, so "Join live" still can.
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("#EXTM3U\n", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1)
        return Response(text, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})
    # Never cached: it is the one file that changes while a recording runs.
    return FileResponse(path, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})


@router.get("/hls/{uid}/{segment}")
def segment(uid: str, segment: str):
    if not _UID.match(uid) or not _SEGMENT.match(segment):
        return Response(status_code=404)
    path = os.path.join(recorder.view_dir(uid), segment)
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="video/mp2t",
                        headers={"Cache-Control": "max-age=86400"})
