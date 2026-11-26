"""What a television client reads: what is live, what was recorded, and the
video itself. The Apple TV app is the first reader, and the web page is the
second.

Everything here is JSON under /api/tv, and the viewing copies under /hls.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.parse
import zoneinfo
from datetime import datetime

import segno
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from .. import (
    adskip,
    announce,
    auth,
    db,
    exposure,
    handover,
    passes,
    paths,
    ratelimit,
    recart,
    recorder,
    sync,
    tls,
    tuner,
)
from . import passes as pass_routes
from . import record
from ._shared import VERSION, _plex, current_user, page

router = APIRouter()
log = logging.getLogger("couchelephant")

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


# ---------- a video address that carries its own pass ----------
#
# AirPlay video has the receiving TV fetch the stream itself, and it has no
# way to send the Authorization header the phone sends. So with sign-in on, a
# paired device that asks to play is given a playlist address with a pass in
# its path (/hlsk/<pass>/<uid>/master.m3u8). The segments are named relative
# to the playlist, so they carry the pass too.
#
# The pass is signed with a random secret that lives in its own file next to
# the database, never inside it, so a copy of the database or a backup cannot
# mint passes. The device's id and stored key hash are in the signed text. It
# stops working when the device is deleted, when the device gets a new key, or
# after 24 hours. Pairing again makes a NEW device and leaves the old one (and
# its passes) alone until an administrator deletes it.

STREAM_PASS_SECONDS = 24 * 3600
_secret_cache: dict = {}


class StreamSecretError(Exception):
    """The signing secret cannot be read or made."""


def _stream_secret() -> bytes:
    path = os.path.join(os.path.dirname(db.DB_PATH), "stream.secret")
    if _secret_cache.get("path") == path:
        return _secret_cache["key"]
    try:
        key = _read_secret(path)
        if len(key) < 32:
            # Made under a temporary name, then linked into place: the link
            # fails if another request got there first, and then everybody
            # reads that one file back and agrees on one key.
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{secrets.token_hex(6)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(tmp)
            key = _read_secret(path)
            if len(key) < 32:
                raise OSError("the secret file is too short")
    except OSError as e:
        log.error("The video pass secret %s cannot be read or made: %s. "
                  "AirPlay video is off until it can.", path, e)
        raise StreamSecretError(str(e)) from e
    _secret_cache.update(path=path, key=key)
    return key


def _read_secret(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def _stream_sig(dev_id: int, key_hash: str, uid: str, exp: int) -> str:
    text = f"{dev_id}.{key_hash}.{uid}.{exp}".encode()
    return hmac.new(_stream_secret(), text, "sha256").hexdigest()[:32]


def stream_pass(device, uid: str) -> str:
    exp = int(time.time()) + STREAM_PASS_SECONDS
    return f"{device['id']}.{exp}.{_stream_sig(device['id'], device['key_hash'], uid, exp)}"


def stream_pass_ok(token: str, uid: str) -> bool:
    try:
        dev_id, exp, sig = token.split(".")
        dev_id, exp = int(dev_id), int(exp)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    row = db.one("SELECT key_hash FROM tv_devices WHERE id = ? AND approved_at IS NOT NULL",
                 (dev_id,))
    if not (row and row["key_hash"]):
        return False
    try:
        return hmac.compare_digest(sig, _stream_sig(dev_id, row["key_hash"], uid, exp))
    except StreamSecretError:
        return False        # already logged; no pass can be checked, so none opens


_HLSK = re.compile(r"^/hlsk/([0-9]+\.[0-9]+\.[0-9a-f]{32})/([0-9a-f]{32})/[^/]+$")


def stream_path_ok(path: str) -> bool:
    """Is this /hlsk/ address one a good pass was made for."""
    m = _HLSK.match(path)
    return bool(m and stream_pass_ok(m.group(1), m.group(2)))


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
            "id": paths.install_id(),
            "pairing_required": needs,
            "paired": bool(dev),
            "can_delete": bool(dev and dev["allow_delete"]),
            "can_change_settings": bool(not needs or (dev and dev["allow_settings"])),
            "recorder": recorder.why_unavailable() is None,
            "recorder_note": recorder.why_unavailable()}


def _is_lan_host(host: str) -> bool:
    """A name or address a phone on the same network can reach."""
    host = (host or "").lower()
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback:
        return False
    return ip.is_private or ip.is_link_local


def _machine_address():
    """This machine's best IPv4 for a phone: 192.168 first, then 10, then
    172.16/12, then anything else (a VPN address is the last resort)."""
    ips = [ipaddress.ip_address(socket.inet_ntoa(a)) for a in announce._addresses()]
    ips = [ip for ip in ips if not announce.is_bridge(str(ip))]

    def rank(ip):
        if ip in ipaddress.ip_network("192.168.0.0/16"):
            return 0
        if ip in ipaddress.ip_network("10.0.0.0/8"):
            return 1
        if ip in ipaddress.ip_network("172.16.0.0/12"):
            return 2
        return 3
    ips.sort(key=rank)             # stable: the list's own order breaks ties
    return str(ips[0]) if ips else None


def lan_url(request: Request):
    """Where a phone should reach this server, or None when there is no
    address it could use."""
    host = request.url.hostname or ""
    scheme = request.url.scheme
    if _is_lan_host(host) and not announce.is_bridge(host):
        # The port the browser used: the Host header's, else the scheme default.
        port = request.url.port
    else:
        # Reached by localhost or a public name: use this machine's own address
        # and the port this server is set to listen on.
        # COUCHELEPHANT_ADDRESS names it outright: the one way a container on
        # Docker's bridge can know the address the rest of the house uses.
        host = os.environ.get("COUCHELEPHANT_ADDRESS", "").strip() or _machine_address()
        if not host:
            return None
        port = paths.port()
        # The server's own port speaks both. Away on means the apps pin HTTPS.
        scheme = "https" if exposure.away_on() else "http"
    return f"{scheme}://{host}" + (f":{port}" if port else "")


NO_ADDRESS = ("This server has no home network address the apps can use. "
              "Type its address in the app instead.")


QR_LOOK = {"dark": "#000", "light": "#fff"}


@router.get("/api/connect.svg")
def connect_svg(request: Request):
    """The QR code the apps scan to connect. Never carries a key or token."""
    url = lan_url(request)
    if url is None:
        return JSONResponse({"ok": False, "detail": NO_ADDRESS}, status_code=409)
    link = "couchelephant://connect?url=" + urllib.parse.quote(url, safe="")
    fp = tls.fingerprint_if_away()
    if fp:
        link += "&fp=" + fp
    svg = segno.make(link).svg_inline(**QR_LOOK)
    # A standalone SVG file needs its namespace; svg_inline leaves it out.
    svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/api/tv/pair")
def pair_start(request: Request, name: str = Form("Apple TV")):
    """A client asks to be let in. It shows the code; a person types the code
    into Settings. The key is handed over only to whoever holds the secret."""
    # From outside, every ask counts, so codes cannot be made by the thousand.
    if exposure.away_on() and not exposure.is_home(request):
        wait = ratelimit.reserve([ratelimit.addr_key(exposure.client_address(request))])
        if wait:
            return JSONResponse({"ok": False, "error": ratelimit.say(wait)},
                                status_code=429,
                                headers={"Retry-After": str(int(wait) + 1)})
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
        "SELECT id, name, code, approved_at, created_at, last_seen, allow_delete, "
        "allow_settings "
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
def device_settings(device_id: int, allow_delete: str = Form(""),
                    allow_settings: str = Form("")):
    """What one television may do. Only what is sent is changed."""
    if not db.one("SELECT 1 FROM tv_devices WHERE id = ?", (device_id,)):
        return JSONResponse({"ok": False, "error": "That television is gone."},
                            status_code=404)
    with db.tx() as c:
        for col, sent in (("allow_delete", allow_delete), ("allow_settings", allow_settings)):
            if sent != "":
                c.execute(f"UPDATE tv_devices SET {col} = ? WHERE id = ?",
                          (1 if sent in ("1", "true", "on") else 0, device_id))
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
    return recart.best(p["art"], p["thumb"]) if p else ""


def _art_of(request, rec):
    """The picture for a recording: its own copy first, the guide second, and
    last a title card, so a tile is never blank."""
    return (recart.best(rec["art"], rec["thumb"]) or _art(rec["program_guid"])
            or (_art_on_now(rec["channel_vcn"]) if rec["kind"] == "live" else "")
            or recart.art_url(_base(request), rec["uid"]))


def _art_on_now(vcn):
    """A live channel has no programme of its own: the picture of what the
    guide says is on it now, so a card for it is not blank."""
    now = int(time.time())
    p = db.one("""SELECT p.art, p.thumb FROM airings a JOIN programs p ON p.guid = a.program_guid
                  WHERE a.channel_vcn = ? AND a.begins_at <= ? AND a.ends_at > ?
                  ORDER BY a.begins_at DESC LIMIT 1""", (vcn, now, now))
    return recart.best(p["art"], p["thumb"]) if p else ""


def _channel_name(vcn):
    """A channel's name when the lineup no longer has it: what a recording
    kept, or at worst its number, never nothing."""
    ch = db.one("SELECT title FROM channels WHERE vcn = ?", (vcn,))
    if ch and ch["title"]:
        return ch["title"]
    r = db.one("SELECT channel_title FROM recordings WHERE channel_vcn = ? "
               "AND COALESCE(channel_title, '') != '' ORDER BY id DESC LIMIT 1", (vcn,))
    return r["channel_title"] if r else f"Channel {vcn}"


def _channel(request, vcn, title):
    has_logo = db.one("SELECT 1 FROM channels WHERE vcn = ? AND logo_path IS NOT NULL "
                      "AND logo_path != ''", (vcn,))
    return {"number": vcn, "name": title or _channel_name(vcn),
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
    return bool(show and (db.one(
        "SELECT 1 FROM programs WHERE grandparent_title = ? AND section = 'sports' "
        "LIMIT 1", (show,)) or db.one(
        "SELECT 1 FROM recordings WHERE show_title = ? AND section = 'sports' "
        "LIMIT 1", (show,))))


def recording_json(request: Request, rec) -> dict:
    view = recorder.view_info(rec["uid"])
    big, small = _names(rec["show_title"], rec["title"], rec["section"] == "sports")
    big = big or "Recording"
    state = rec["state"]
    return {
        "id": rec["id"],
        "kind": rec["kind"],
        "state": state,
        "recorded_by": "couchelephant",
        "title": big, "subtitle": small,
        "channel": _channel(request, rec["channel_vcn"], rec["channel_title"]),
        "art": _art_of(request, rec),
        "begins_at": rec["begins_at"], "ends_at": rec["ends_at"],
        "started_at": rec["started_at"], "ended_at": rec["ended_at"],
        # How much there is to watch right now, which for a recording still
        # being made is less than how long it has been running.
        "seconds": round(view["seconds"], 1),
        "complete": view["complete"],
        "position": round(rec["position"] or 0, 1),
        "playable": view["segments"] > 0 or state in ("in_plex",),
        # The master playlist, which tells a player the captions are there.
        "playlist": f"{_base(request)}/hls/{rec['uid']}/master.m3u8",
        "error": rec["error"] if state == "failed" else None,
        # A finished recording that Plex has not been given yet, and why, and
        # how long until the automatic try. "Retry now" does not wait for it.
        "handover_error": rec["error"] if state == "finished" and rec["error"] else None,
        "retry_in": (max(0, (rec["tried_at"] or 0) + handover.RETRY_SECONDS - int(time.time()))
                     if state == "finished" and rec["error"] else None),
        # Sports overtime: how long a game ran past its padding, or why it
        # was not followed.
        "note": rec["ot_note"] if rec["ot_state"] in ("in", "post") else None,
        # What a list of recordings sorts and filters by.
        "group": rec["show_title"] or rec["title"] or "",
        "category": _category(rec),
        "season": rec["season"], "episode": rec["episode"],
        "airing_id": rec["airing_id"],
        # How long a player may stay paused on live TV before it carries on.
        "pause_limit": recorder.pause_limit() if rec["kind"] == "live" else None,
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
            "art": recart.art_url(_base(request), f"plex-{g['id']}"),
            "begins_at": g["begins_at"], "ends_at": g["ends_at"],
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
                    "art": recart.best(r["art"], r["thumb"]),
                    "begins_at": r["begins_at"], "ends_at": r["ends_at"]})
    return out


# ---------- the web page ----------

@router.get("/home", response_class=HTMLResponse)
def home_page(request: Request):
    """The first tab: the same three lists the television app opens on.

    It is what CouchElephant DVR has to show, so an install without it is sent
    to the guide, which is its first tab.
    """
    if not recorder.available():
        return RedirectResponse("/guide", status_code=303)
    return page(request, "home.html")


@router.get("/live")
def live_redirect():
    """This page was called Live TV, at this address, before it became Home."""
    return RedirectResponse("/home", status_code=301)


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


@router.get("/api/tv/recordings/{rec_id}/stream")
def recording_stream(request: Request, rec_id: int):
    """The playlist address to play this recording from, asked for when play
    starts. With sign-in on and a paired device it carries a pass (see above),
    so a TV that fetches the stream over AirPlay can open it."""
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    uid = rec["uid"]
    url = f"{_base(request)}/hls/{uid}/master.m3u8"
    if auth.mode() != "none":
        dev = device_for(request)
        if dev and dev["key_hash"]:
            try:
                url = f"{_base(request)}/hlsk/{stream_pass(dev, uid)}/{uid}/master.m3u8"
            except StreamSecretError:
                return JSONResponse({"ok": False, "error":
                                     "AirPlay video is not available: the server cannot "
                                     "read its video pass secret. See the server log."},
                                    status_code=503)
    return {"ok": True, "playlist": url}


@router.get("/api/tv/recordings/{rec_id}/breaks")
def recording_breaks(rec_id: int):
    """Where the ads are, for a Skip button. A player asks again every half
    minute while `growing` is true, because breaks are added as they are found."""
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    return adskip.breaks_for(rec)


@router.get("/api/tv/recordings/{rec_id}/now")
def recording_now(request: Request, rec_id: int, seconds: float = 0):
    """What is on the screen at this point of a recording or a live channel:
    the guide's programme on that channel at that moment. A live channel moves
    on to the next programme while it plays, so the player asks each time."""
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    start = rec["started_at"] or rec["starts_at"] or rec["begins_at"] or int(time.time())
    at = int(start + max(0.0, seconds))
    # A game in overtime is still the game, whatever the guide put next.
    game = db.one(
        "SELECT * FROM recordings WHERE kind = 'recording' AND channel_vcn = ? "
        "AND (ot_state = 'in' OR (ot_state = 'post' AND stops_at > ot_base)) "
        "AND ends_at <= ? AND stops_at > ? "
        "ORDER BY id = ? DESC LIMIT 1", (rec["channel_vcn"], at, at, rec["id"]))
    a = None if game else db.one(
        """SELECT a.begins_at, a.ends_at, a.program_guid, p.title, p.grandparent_title,
                  p.summary, p.section, p.season, p.episode, p.year, p.content_rating
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.channel_vcn = ? AND a.begins_at <= ? AND a.ends_at > ?
           ORDER BY a.begins_at DESC LIMIT 1""", (rec["channel_vcn"], at, at))
    # In the padding before or after a recording this finds the neighbour,
    # which is what is on screen then.
    if a:
        big, small = _names(a["grandparent_title"], a["title"], a["section"] == "sports")
        prog = {"title": big, "subtitle": small, "summary": a["summary"] or "",
                "begins_at": a["begins_at"], "ends_at": a["ends_at"],
                "season": a["season"], "episode": a["episode"], "year": a["year"],
                "rating": a["content_rating"] or ""}
    else:
        # The guide has forgotten it, which it does once a programme has
        # aired, or the game ran past it. The recording kept its own name and
        # description.
        rec = game or rec
        big, small = _names(rec["show_title"], rec["title"], rec["section"] == "sports")
        p = db.one("SELECT summary, content_rating FROM programs WHERE guid = ?",
                   (rec["program_guid"],)) if rec["program_guid"] else None
        prog = {"title": big, "subtitle": small,
                "summary": rec["summary"] or (p["summary"] if p else "") or "",
                "begins_at": rec["begins_at"], "ends_at": rec["ends_at"],
                "season": rec["season"], "episode": rec["episode"], "year": rec["year"],
                "rating": rec["content_rating"] or (p["content_rating"] if p else "") or ""}
    prog["channel"] = _channel(request, rec["channel_vcn"], rec["channel_title"])
    return {"ok": True, "program": prog}


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
        "summary": a["summary"] or "", "art": recart.best(a["art"], a["thumb"]),
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


@router.get("/api/tv/record/options")
async def tv_record_options(airing_id: str, scope: str = "series", team_id: str = ""):
    """What "Record every episode" or "Record every <team> game" can be set
    to, before the pass is made: the web's options step, for an app.

    The same answer as the web's `/api/record/options`, so the choices are
    Plex's own and never a copy, with only the recurring choices left in,
    because the person has already said they want all of them. `pick` is the
    one they pressed: the team's own rule, or the series rule.
    """
    got = await record.api_record_options(airing_id)
    body = json.loads(got.body)
    if got.status_code != 200 or not body.get("ok", True) or "templates" not in body:
        body.setdefault("ok", False)
        return JSONResponse(body, status_code=got.status_code)
    recurring = [t for t in body["templates"] if not t["one_shot"]]
    if not recurring:
        return JSONResponse({"ok": False, "error":
                             "Plex offers no way to record every one of these."},
                            status_code=400)
    team = db.one("SELECT name FROM teams WHERE id = ?", (team_id or 0,)) \
        if scope == "team" else None
    teams = [t.get("name") for t in db.unjs(record._airing_row(airing_id)["teams"]) or []
             if t.get("name")]
    if team:
        named = [t for t in recurring if team["name"] in (t["title"] or "")]
    else:
        # The series rule is the recurring one that names no team. A game's
        # league rule names none either, and comes before the teams, so it
        # wins only where there is no series rule at all.
        named = [t for t in recurring if not any(n in (t["title"] or "") for n in teams)]
    body["templates"] = recurring
    body["pick"] = (named or recurring)[0]["index"]
    # A television plays what CouchElephant records while it records, so that
    # is the default here wherever it can run, as it always was for the apps.
    if body["recorder"]["available"]:
        body["recorder"]["default"] = "couchelephant"
    return body


@router.post("/api/tv/record/all")
async def tv_record_all(airing_id: str = Form(...), template: int = Form(...),
                        settings: str = Form("{}"), networks: str = Form("[]"),
                        channels: str = Form("[]"),
                        recorder_choice: str = Form("couchelephant", alias="recorder")):
    """Make the pass with the options chosen in the app, exactly as the web's
    Schedule button does: the same route, so the two cannot drift."""
    return await record.api_record(airing_id=airing_id, template=template,
                                   settings=settings, networks=networks,
                                   channels=channels, recorder_choice=recorder_choice)


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
            "pass_id": str(r.get("pass_id") or ""),
            # One broadcast booked here can be handed to the other recorder.
            # One a pass booked follows its pass, and one Plex booked by its
            # own rule is Plex's. A recording already running stays put.
            "movable": (kind == "one" and r["who"] == "ce" and bool(r["airing_id"])
                        and r["status"] == "scheduled"),
        })
    upcoming.sort(key=lambda u: u["begins_at"] or 0)
    recorded = [recording_json(request, r) for r in db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('recording', 'finished', 'moving', 'in_plex')")]
    recorded.sort(key=lambda r: (r["group"].lower(), r["begins_at"] or 0))
    return {"ok": True, "now": now, "can_delete": _may_delete(request),
            "upcoming": upcoming, "recorded": recorded,
            "passes": _passes(),
            "networks": [r["network"] for r in db.query(
                "SELECT DISTINCT network FROM channels WHERE network IS NOT NULL "
                "AND network != '' ORDER BY network COLLATE NOCASE")],
            "recorder_available": recorder.available()}


@router.get("/api/tv/schedule")
def tv_schedule(request: Request, offset: int = 0, limit: int = 40,
                start: int = 0, end: int = 0):
    """The whole schedule the web page's Agenda and Calendar draw: bookings
    and failed ones, in pages, nearest to now outward, or inside a window of
    time for a month. The same rows as /api/schedule, with the channel shaped
    the way the apps read it."""
    limit = max(1, min(limit, 1000))
    rows = pass_routes._schedule_rows(limit=limit + 1, offset=offset,
                                      start=start or None, end=end or None)
    more = len(rows) > limit
    rows = rows[:limit]
    titles = {c["vcn"]: c["title"] for c in db.query("SELECT vcn, title FROM channels")}
    out = []
    for r in rows:
        r = dict(r)
        r["channel"] = _channel(request, r["vcn"], titles.get(r["vcn"], ""))
        out.append(r)
    return {"ok": True, "rows": out, "more": more}


@router.get("/api/tv/schedule/failure")
def tv_schedule_failure(airing_id: str = ""):
    """What the web's failure panel shows for a booking Plex refused."""
    return pass_routes.api_schedule_failure(airing_id=airing_id)


@router.post("/api/tv/schedule/retry")
async def tv_schedule_retry(airing_id: str = Form(...)):
    """Try that booking again, as the web's Try again button does."""
    return await pass_routes.api_schedule_retry(airing_id=airing_id)


@router.get("/api/tv/expectations")
def tv_expectations():
    """What every pass is still waiting for. The date text is made here,
    at the precision the guide gave, so a client never invents a day."""
    return pass_routes.api_expectations()


# Padding a television offers. The same steps as the web panel.
PAD_START = [0, 1, 2, 5, 10, 15, 30]
PAD_END = [0, 5, 15, 30, 45, 60, 90, 120, 180]


def _whole(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _passes() -> list[dict]:
    """Every pass and every Plex rule, shaped for a television.

    Built from the web app's own list, so the two cannot disagree about what
    exists. `tab` is which of the television's lists it belongs in: a team
    pass is a sports pass, and everything else that keeps recording is a
    season pass.
    """
    rules = json.loads(pass_routes.api_rules().body)["rules"]
    out = []
    for r in rules:
        ours = r["who"] == "ce"
        prefs = r.get("prefs") or {}
        out.append({
            "id": str(r["id"]), "who": r["who"], "kind": r["kind"],
            "tab": "sports" if r["kind"] == "team" or r.get("icon") == "sports" else "season",
            "title": r["title"] or "", "detail": r.get("detail") or "",
            "recorder": r.get("recorder") or "plex",
            "enabled": bool(r.get("enabled", True)),
            "networks": r.get("networks") or [], "channels": r.get("channels") or [],
            "channels_per_game": r.get("channels_per_game") or "one",
            "start_minutes": _whole(prefs.get("startOffsetMinutes")),
            "end_minutes": _whole(prefs.get("endOffsetMinutes")),
            "booked": r.get("booked") or 0,
            "upcoming": r.get("upcoming") or 0,
            "lost_id": r.get("lost_id"), "can_follow": bool(r.get("can_follow")),
            # A Plex rule is Plex's own. It can be removed here and nothing else.
            "editable": ours,
        })
    out.sort(key=lambda p: p["title"].lower())
    return out


@router.get("/api/tv/passes/find")
def tv_pass_find(q: str = "", tab: str = "season"):
    """What a new pass could follow. `tab` is "season" for series and movies,
    "sports" for teams. Shows in the guide come first, then what TVmaze or
    TMDB know that the guide has not reached yet."""
    q = (q or "").strip()
    out = []
    if tab == "sports":
        teams = json.loads(pass_routes.api_teams(q=q).body)["teams"]
        for t in teams[:60]:
            out.append({"kind": "team", "id": str(t["id"] or ""), "title": t["name"],
                        "detail": (t["league"] or "") + (" - plays this week" if t["playing"] else ""),
                        "followed": t["followed"]})
        return {"ok": True, "results": out}
    if not q:
        # The busiest titles in a guide are Paid Programming and Shopping.
        return {"ok": True, "results": []}
    followed = {r["series_title"] for r in db.query(
        "SELECT series_title FROM passes WHERE kind = 'series'")}
    tz = _zone()
    for r in json.loads(pass_routes.api_series(q=q).body)["series"]:
        when = datetime.fromtimestamp(r["next_at"] or 0, tz).strftime("%a %b %-d")
        out.append({"kind": "series", "id": "", "title": r["name"],
                    "detail": f"{r['airings']} in the guide, next {when}",
                    "followed": r["name"] in followed})
    seen = {o["title"].lower() for o in out}
    for a in json.loads(pass_routes.api_announced(q=q).body)["announced"]:
        if a["title"].lower() in seen:
            continue
        bits = [b for b in (a.get("subtitle"), a.get("network"), a.get("when")) if b]
        out.append({"kind": "announced", "id": f"{a['source']}:{a['source_id']}",
                    "title": a["title"], "detail": ", ".join(bits) or "Not in the guide yet",
                    "followed": a["title"] in followed})
    return {"ok": True, "results": out}


def _zone():
    try:
        return zoneinfo.ZoneInfo(db.get_setting("timezone") or "UTC")
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


@router.post("/api/tv/passes/new")
def tv_pass_new(kind: str = Form(...), title: str = Form(""), id: str = Form("")):
    """Start a pass from the app's + button. Recorded by CouchElephant where
    it can run, because that is what a television can play."""
    who = "couchelephant" if recorder.available() else None
    title = (title or "").strip()
    if kind == "team":
        t = db.one("SELECT * FROM teams WHERE id = ?", (id,)) if id else None
        team = dict(t) if t else {"id": None, "name": title}
        if not team["name"]:
            return JSONResponse({"ok": False, "error": "Pick a team."}, status_code=400)
        pass_id, label, made = record._make_pass("team", team=team, update=False, who=who)
    elif kind == "announced":
        source, _, source_id = id.partition(":")
        got = pass_routes.api_announced_follow(source=source, source_id=source_id,
                                               title=title)
        body = json.loads(got.body)
        if not body.get("ok"):
            return JSONResponse(body, status_code=got.status_code)
        if who:
            with db.tx() as c:
                c.execute("UPDATE passes SET recorder = ? WHERE id = ?",
                          (who, body["pass_id"]))
        return {"ok": True, "message": f"Following {title}. It records once the "
                                       "guide carries it."}
    elif kind == "series":
        if not title:
            return JSONResponse({"ok": False, "error": "Pick a series."}, status_code=400)
        pass_id, label, made = record._make_pass("series", series=title, update=False,
                                                 who=who)
    else:
        return JSONResponse({"ok": False, "error": "Unknown kind of pass."},
                            status_code=400)
    if not made:
        with db.tx() as c:
            c.execute("UPDATE passes SET enabled = 1 WHERE id = ?", (pass_id,))
    done = passes.run_passes()
    booked = len([d for d in done if d["action"] == "scheduled"])
    if not made:
        return {"ok": True, "message": f"Already following {label}. It is switched on."}
    return {"ok": True, "message": f"Following {label}. {booked} set to record so far."}


@router.get("/api/tv/passes/options")
def tv_pass_options(kind: str, id: str = "", title: str = ""):
    """What a new pass from the + button can be set to, before it is made:
    the web's Add panel, for an app, in the shape `/api/tv/record/options`
    has, so the apps draw both with one screen.

    The templates come from `/api/rules/options`, as the web's panel takes
    them. Something the guide has not reached yet (an announced series, a
    team not playing this week) gets the settings each booking will carry.
    """
    if kind == "series":
        got = pass_routes.api_rule_options(kind="series", series=title)
    elif kind == "team" and id:
        got = pass_routes.api_rule_options(kind="team", team_id=id)
    elif kind in ("team", "announced"):
        got = pass_routes.api_rule_options(kind="any", ce_pass=1,
                                           sporty=1 if kind == "team" else 0)
    else:
        return JSONResponse({"ok": False, "error": "Unknown kind of pass."},
                            status_code=400)
    body = json.loads(got.body)
    if not body.get("ok"):
        return JSONResponse(body, status_code=got.status_code if got.status_code != 200
                            else 400)
    if body.get("sporty") and kind == "team":
        # Sport runs over, so the padding is filled in, as the web does it.
        pad = body.get("sports_padding") or {}
        for t in body["templates"]:
            for s in t["settings"]:
                if s["id"] in pad:
                    s["value"] = str(pad[s["id"]])
    sources = json.loads(pass_routes.api_sources().body)
    available = recorder.available()
    return {"ok": True, "title": title, "templates": body["templates"],
            "pick": body["templates"][0]["index"] if body["templates"] else 0,
            "recorder": {"available": available,
                         "default": "couchelephant" if available else "plex"},
            "networks": sources["networks"], "channels": sources["channels"]}


@router.post("/api/tv/passes/create")
async def tv_pass_create(kind: str = Form(...), id: str = Form(""), title: str = Form(""),
                         template: int = Form(0), settings: str = Form("{}"),
                         networks: str = Form("[]"), channels: str = Form("[]"),
                         recorder_choice: str = Form("couchelephant", alias="recorder"),
                         channels_per_game: str = Form("")):
    """Make the pass with the options chosen in the app. A series or a team
    goes through the web's own Add route, so the two cannot drift."""
    title = (title or "").strip()
    if kind in ("series", "team"):
        return await pass_routes.api_rule_create(
            kind=kind, team_id=id if kind == "team" else "",
            series=title if kind == "series" else "", networks=networks,
            channels=channels, template=template, settings=settings, filter="",
            name="", confirm="", team=title if kind == "team" else "",
            recorder=recorder_choice, channels_per_game=channels_per_game)
    if kind != "announced":
        return JSONResponse({"ok": False, "error": "Unknown kind of pass."},
                            status_code=400)
    if not title:
        return JSONResponse({"ok": False, "error": "Pick a series."}, status_code=400)
    who = pass_routes._who_records(recorder_choice)
    if who == "couchelephant" and record.recorder_off():
        return record.recorder_off()
    # The pass with its options first, then the follow, which finds it and
    # adds what TVmaze or TMDB expect.
    _, label, made = await asyncio.to_thread(
        pass_routes._make_pass, "series", None, title, db.unjs(networks, []) or [],
        db.unjs(channels, []) or [], pass_routes._pass_prefs(db.unjs(settings, {}) or {}),
        False, None, "", who)
    if not made:
        return JSONResponse({"ok": False, "error": f"You already follow {label}."},
                            status_code=409)
    source, _, source_id = id.partition(":")
    got = pass_routes.api_announced_follow(source=source, source_id=source_id, title=title)
    if not json.loads(got.body).get("ok"):
        return got
    return {"ok": True, "message": f"Following {label}. It records once the guide "
                                   "carries it."}


@router.post("/api/tv/passes/{pass_id}")
async def tv_pass_edit(pass_id: int, recorder_choice: str = Form("", alias="recorder"),
                       enabled: str = Form(""), networks: str = Form(""),
                       start_minutes: str = Form(""), end_minutes: str = Form(""),
                       channels_per_game: str = Form("")):
    """Change a pass from a television. Only what is sent is changed.

    The web app's own edit clears whatever it is not sent, which is right for
    a form that always sends everything and wrong for a remote that changes
    one thing at a time.
    """
    p = db.one("SELECT * FROM passes WHERE id = ?", (pass_id,))
    if not p:
        return JSONResponse({"ok": False, "error": "That pass is gone."}, status_code=404)
    if recorder_choice == "couchelephant" and record.recorder_off():
        return record.recorder_off()
    who = recorder_choice if recorder_choice in ("plex", "couchelephant") else p["recorder"]
    on = p["enabled"] if enabled == "" else (0 if enabled in ("0", "false") else 1)
    nets = db.unjs(networks, None) if networks else None
    per_game, bad = pass_routes._per_game(channels_per_game, p["kind"])
    if bad:
        return JSONResponse({"ok": False, "error": bad}, status_code=400)
    prefs = dict(db.unjs(p["prefs"], {}) or {})
    for key, sent, allowed in (("startOffsetMinutes", start_minutes, PAD_START),
                               ("endOffsetMinutes", end_minutes, PAD_END)):
        if sent != "":
            try:
                value = int(sent)
            except ValueError:
                continue
            if value in allowed:
                prefs[key] = str(value)
    with db.tx() as c:
        c.execute("UPDATE passes SET recorder = ?, enabled = ?, prefs = ?, "
                  "networks = COALESCE(?, networks), "
                  "channels_per_game = COALESCE(?, channels_per_game) WHERE id = ?",
                  (who, on, db.js(prefs), db.js(nets) if nets is not None else None,
                   per_game, pass_id))
    moved = 0
    if passes.pass_recorder(p) != ("couchelephant" if who == "couchelephant" else "plex"):
        # Who records changed, so what the pass already booked goes with it.
        try:
            moved, done = await asyncio.to_thread(passes.change_recorder, pass_id)
        except Exception as e:
            return JSONResponse({"ok": False, "error":
                                 "The pass is saved, and its booked recordings could not "
                                 f"be moved: {e}"}, status_code=502)
    else:
        done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    try:
        # The recordings Plex already holds for this pass take the new padding
        # now, and not at the next sync.
        with _plex() as plex:
            await asyncio.to_thread(sync.sync_recordings, plex)
            await asyncio.to_thread(sync.check_bookings, plex)
    except Exception:
        # The pass is saved. Plex not answering for a moment must not fail it,
        # and the next sync carries the change.
        pass
    if moved:
        to = "CouchElephant" if who == "couchelephant" else "Plex"
        return {"ok": True, "message": f"Saved. {moved} booked recording(s) moved to {to}."}
    return {"ok": True, "message": "Saved." + (f" {made} more set to record." if made else "")}


@router.post("/api/tv/passes/{pass_id}/delete")
def tv_pass_delete(pass_id: str, who: str = Form("ce"),
                   cancel_upcoming: str = Form("0")):
    """Stop following. `cancel_upcoming=1` also cancels what the pass booked
    and has not started, and the answer says how many. Without it, what is
    already booked stays, as before."""
    if who == "plex":
        return pass_routes.api_plexrule_delete(pass_id)
    if pass_id.startswith("lost:") and pass_id[5:].isdigit():
        # A lost pass's row, deleted the usual way by an older app.
        if cancel_upcoming in ("1", "true", "yes"):
            return tv_lost_cancel(int(pass_id[5:]))
        return {"ok": True, "cancelled": 0, "message": "Nothing changed. Use Cancel all "
                "or Follow again for a pass that was deleted."}
    if not pass_id.isdigit():
        return JSONResponse({"ok": False, "error": "No such pass."}, status_code=404)
    body, code = pass_routes.delete_pass(int(pass_id),
                                         cancel_upcoming in ("1", "true", "yes"))
    return JSONResponse(body, status_code=code)


@router.get("/api/tv/passes/lost")
def tv_passes_lost():
    """Bookings left behind by passes that were deleted, one per lost pass."""
    return {"ok": True, "lost": passes.orphans()}


@router.post("/api/tv/passes/lost/{lost_id}/cancel")
def tv_lost_cancel(lost_id: int):
    """Cancel every upcoming booking of a pass that is gone."""
    try:
        n = passes.cancel_upcoming(lost_id)
    except Exception as e:
        done = getattr(e, "done", 0)
        what = ("A recording could not be cancelled" if getattr(e, "where", "") == "recorder"
                else "Plex could not cancel the recordings")
        return JSONResponse({"ok": False, "cancelled": done, "error":
                             what + "." + (f" {done} were already cancelled." if done else "")
                             + f" ({e})"}, status_code=502)
    left = passes.keyless(lost_id)
    msg = f"{n} upcoming recording(s) cancelled." if n else "Nothing to cancel."
    if left:
        msg += f" {left} booking(s) in Plex have to be removed in Plex itself."
    return {"ok": True, "cancelled": n, "message": msg}


@router.post("/api/tv/passes/lost/{lost_id}/follow")
def tv_lost_follow(lost_id: int):
    """Make the series pass again, with default settings, and hand it the
    bookings. Only a series can be rebuilt: it is named by its title, which
    the bookings carry. A team is not, so that is refused rather than guessed."""
    lost = next((o for o in passes.orphans() if o["lost_id"] == lost_id), None)
    if not lost:
        return JSONResponse({"ok": False, "error": "Nothing is left of that pass."},
                            status_code=404)
    if not lost["can_follow"]:
        return JSONResponse({"ok": False, "error": "This pass cannot be rebuilt from "
                             "its recordings. Cancel them, or follow the team again."},
                            status_code=409)
    pid, label, _ = record._make_pass("series", series=lost["title"], update=False,
                                      who=lost["recorder"])
    with db.tx() as c:
        for table in ("recordings", "our_grabs"):
            c.execute(f"UPDATE {table} SET pass_id = ?, pass_uid = "
                      "(SELECT uid FROM passes WHERE id = ?) WHERE pass_id = ?",
                      (pid, pid, lost_id))
    passes.run_passes()
    return {"ok": True, "pass_id": pid, "message": f"Following {label} again."}


@router.post("/api/tv/record/move")
async def tv_record_move(airing_id: str = Form(...),
                         recorder_choice: str = Form(..., alias="recorder")):
    """Hand one booked broadcast to the other recorder.

    Booked with the new one first is not possible: each recorder refuses a
    broadcast the other already has. So it is cancelled, then booked, and if
    the booking fails the reply says the recording is now not set at all.
    """
    row = record._airing_row(airing_id)
    if not row:
        return JSONResponse({"ok": False, "error":
                             "That broadcast is no longer in the guide."}, status_code=404)
    to_ours = recorder_choice == "couchelephant"
    mine = recorder.booked(row)
    if to_ours:
        if mine:
            return {"ok": True, "message": "CouchElephant already records this."}
        if record.recorder_off():
            return record.recorder_off()
        if not db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (airing_id,)):
            return JSONResponse({"ok": False, "error":
                                 "Plex booked this by its own rule, so it is Plex's to "
                                 "record."}, status_code=409)
        gone = await record.api_record_cancel(airing_id=airing_id)
        if gone.status_code >= 400:
            return gone
        made = await asyncio.to_thread(record._record_ourselves, row, {})
        if made.status_code >= 400:
            said = json.loads(made.body).get("error", "")
            return JSONResponse({"ok": False, "error":
                                 "Plex's booking was cancelled, and CouchElephant could "
                                 f"not book it: {said} It is not set to record now."},
                                status_code=409)
        return {"ok": True, "message": "CouchElephant records this now."}
    if not mine:
        return {"ok": True, "message": "CouchElephant does not have this one."}
    if mine["state"] != "scheduled":
        return JSONResponse({"ok": False, "error":
                             "It is already recording. It stays with CouchElephant."},
                            status_code=409)
    await asyncio.to_thread(recorder.cancel, mine["id"])
    try:
        with _plex() as plex:
            await asyncio.to_thread(passes._schedule, plex, row, None, "manual")
            await asyncio.to_thread(sync.sync_recordings, plex)
    except Exception as e:
        return JSONResponse({"ok": False, "error":
                             "CouchElephant's booking was cancelled, and Plex could not "
                             f"book it: {e} It is not set to record now."}, status_code=409)
    return {"ok": True, "message": "Plex records this now."}


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


@router.post("/api/tv/recordings/{rec_id}/retry")
def tv_recording_retry(rec_id: int):
    """Give a finished recording to Plex again now, without waiting for the
    automatic try, and say what happened. `outcome` is one of `started`,
    `queued` (another is being copied first), `copying` (this one already is),
    `converting` (the viewing copy is still being made), `watched` (somebody
    watched it a moment ago, so it waits for them) or `held` (the handover is
    not set up and the message says what is missing: a problem to fix, not a
    wait)."""
    rec = recorder.get(rec_id)
    if not rec or rec["kind"] != "recording":
        return JSONResponse({"ok": False, "error": "That recording is gone."},
                            status_code=404)
    if rec["state"] == "moving":
        return {"ok": True, "outcome": "copying",
                "message": "It is being copied to Plex right now."}
    if rec["state"] != "finished":
        return JSONResponse({"ok": False, "error":
                             "That recording is not waiting to go to Plex."},
                            status_code=409)
    if recorder.watched_recently(rec):
        return {"ok": True, "outcome": "watched", "message":
                "Somebody watched it a moment ago. It goes to Plex when they stop."}
    if recorder.busy(rec_id):
        return {"ok": True, "outcome": "converting", "message":
                "Its viewing copy is still being made. It goes to Plex when that is done."}
    recorder._set(rec_id, tried_at=0)
    started = handover.tick()
    if started == rec_id:
        return {"ok": True, "outcome": "started", "message": "Copying it to Plex now."}
    held = handover.why_unavailable(recorder.get(rec_id))
    if held:
        # Not set up: the real reason, not a wait for somebody else's copy.
        return {"ok": True, "outcome": "held", "message": held}
    w = handover._worker
    if w and w.is_alive():
        return {"ok": True, "outcome": "queued", "message":
                "Another recording is being copied first. This one goes next."}
    return {"ok": True, "outcome": "queued", "message":
            "It is next in line and is tried again in a moment."}


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


@router.post("/api/tv/live/{rec_id}/stop")
def live_stop(rec_id: int):
    """The player closed. Gives the tuner back without waiting out the pause limit."""
    recorder.stop_live(rec_id)
    return {"ok": True}


@router.post("/api/tv/live/{rec_id}/keepalive")
def live_keepalive(rec_id: int, paused: str = Form("")):
    """The player is still on this channel. `paused` is "1" when the person
    paused it, which is what holds the tuner for the pause limit; "0" while it
    plays. An app that sends neither is held as before."""
    rec = recorder.get(rec_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "That session has ended."},
                            status_code=404)
    recorder.watching(rec_id, paused={"1": True, "0": False}.get(paused))
    return {"ok": True}


# ---------- the video ----------

@router.get("/hls/{uid}/master.m3u8")
def master_playlist(uid: str, start: str = ""):
    """One picture and the broadcast's captions, CC1, inside it. Without this
    line Apple's player never offers captions, even though they are there."""
    if not _UID.match(uid) or not os.path.exists(recorder.playlist_path(uid)):
        return Response(status_code=404)
    variant = "index.m3u8" + ("?start=0" if start == "0" else "")
    # No language: CC1 is whatever the station captions in, English on most,
    # French or Spanish on others, and a wrong label is worse than none.
    text = ("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-INDEPENDENT-SEGMENTS\n"
            '#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="Captions",'
            'INSTREAM-ID="CC1",DEFAULT=NO,AUTOSELECT=YES\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,CLOSED-CAPTIONS="cc"\n'
            f"{variant}\n")
    return Response(text, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


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


@router.get("/hlsk/{token}/{uid}/master.m3u8")
def master_playlist_k(token: str, uid: str, start: str = ""):
    return master_playlist(uid, start)


@router.get("/hlsk/{token}/{uid}/index.m3u8")
def playlist_k(token: str, uid: str, start: str = ""):
    return playlist(uid, start)


@router.get("/hlsk/{token}/{uid}/{segment}")
def segment_k(token: str, uid: str, segment: str):
    return segment_(uid, segment)


@router.get("/hls/{uid}/{segment}")
def segment_(uid: str, segment: str):
    if not _UID.match(uid) or not _SEGMENT.match(segment):
        return Response(status_code=404)
    path = os.path.join(recorder.view_dir(uid), segment)
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="video/mp2t",
                        headers={"Cache-Control": "max-age=86400"})
