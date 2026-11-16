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

from .. import auth, db, recorder, tuner
from ._shared import VERSION, page

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
    return {"ok": True, "name": "CouchElephant", "version": VERSION,
            "pairing_required": needs,
            "paired": bool(device_for(request)) if needs else True,
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
        c.execute("INSERT INTO tv_devices (name, code, poll_hash, created_at) "
                  "VALUES (?, ?, ?, ?)", (name[:60], code, _hash(secret), now))
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
        "SELECT id, name, code, approved_at, created_at, last_seen FROM tv_devices "
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


def _names(show, title):
    """The big line and the small line. For sport the game is the big one."""
    if show and title and show != title:
        return title, show
    return title or show or "", ""


def recording_json(request: Request, rec) -> dict:
    view = recorder.view_info(rec["uid"])
    big, small = _names(rec["show_title"], rec["title"])
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
    }


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
        big, small = _names(g["parent_title"], g["title"])
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
                  p.guid, p.title, p.grandparent_title, p.thumb, p.art
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at <= ? AND a.ends_at > ? AND COALESCE(a.drm, 0) = 0""",
        (now, now))
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: _vcn_key(r["channel_vcn"])):
        vcn = r["channel_vcn"]
        if vcn in seen or vcn in skip:
            continue
        seen.add(vcn)
        big, small = _names(r["grandparent_title"], r["title"])
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


# ---------- live ----------

@router.post("/api/tv/live/{vcn}")
def live_start(request: Request, vcn: str):
    note = recorder.why_unavailable()
    if note:
        return JSONResponse({"ok": False, "error": note}, status_code=400)
    now = int(time.time())
    on = db.one(
        """SELECT p.title, p.grandparent_title, a.channel_title FROM airings a
           JOIN programs p ON p.guid = a.program_guid
           WHERE a.channel_vcn = ? AND a.begins_at <= ? AND a.ends_at > ?""",
        (vcn, now, now))
    try:
        tuner.stream_url(vcn)          # refuses an encrypted or ATSC 3.0 channel
    except tuner.TunerError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    title = _names(on["grandparent_title"], on["title"])[0] if on else f"Channel {vcn}"
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
