"""What a recording needs to be shown, kept on the recording.

The guide is a cache. It forgets a programme once it has aired, so a recording
that read its picture, description or teams from the guide went blank a few
days after it was made. Each recording now carries its own copy: filled when it
is booked, refreshed while the guide still has the programme, fetched again
from Plex when the guide is gone, and, when no picture exists anywhere, drawn
as a title card so no tile is ever empty.
"""
import functools
import io
import logging
import os
import threading
import time
import zlib

import httpx

from . import db, health

log = logging.getLogger("couchelephant.recart")

_DEFAULT_DISCOVER = "https://discover.provider.plex.tv"
DISCOVER_URL = os.environ.get("COUCHELEPHANT_DISCOVER_URL", _DEFAULT_DISCOVER)
# A picture address is sent to every client, so only a secure one is used.
if not DISCOVER_URL.startswith("https://"):
    DISCOVER_URL = _DEFAULT_DISCOVER
FETCH_TIMEOUT = 10.0         # seconds a call to Plex may take, never the 120 the rest use
MAX_BODY = 256 * 1024        # a metadata answer is a few KB; refuse anything near this
MAX_URL = 1000
PASS_SECONDS = 20.0          # one pass stops starting fetches after this long
DAILY_CAP = 20               # recordings the daily sweep looks at per pass
# A finished recording: the ones whose picture matters and will not come back.
SHOWN = "('recording', 'finished', 'moving', 'in_plex', 'failed')"
PER_PASS = 2                 # fetches per minute, so Plex is never hammered
DAY = 86400


# ---------- copying from the guide ----------

_REFRESH = (
    # The guide's picture wins while it has one: it is the newest. A blank one
    # never replaces what is kept.
    """UPDATE recordings SET
         art = COALESCE((SELECT NULLIF(ce_public(p.art), '') FROM programs p
                         WHERE p.guid = recordings.program_guid), art),
         thumb = COALESCE((SELECT NULLIF(ce_public(p.thumb), '') FROM programs p
                           WHERE p.guid = recordings.program_guid), thumb)
       WHERE program_guid IS NOT NULL AND EXISTS (
         SELECT 1 FROM programs p WHERE p.guid = recordings.program_guid
           AND ((NULLIF(ce_public(p.art), '') IS NOT NULL
                 AND p.art IS NOT COALESCE(recordings.art, ''))
             OR (NULLIF(ce_public(p.thumb), '') IS NOT NULL
                 AND p.thumb IS NOT COALESCE(recordings.thumb, ''))))""",
    # What is already kept is never overwritten, only filled in.
    """UPDATE recordings SET
         summary = COALESCE(NULLIF(summary, ''), (SELECT NULLIF(p.summary, '') FROM programs p
                            WHERE p.guid = recordings.program_guid)),
         content_rating = COALESCE(NULLIF(content_rating, ''),
                            (SELECT NULLIF(p.content_rating, '') FROM programs p
                             WHERE p.guid = recordings.program_guid)),
         teams = COALESCE(NULLIF(teams, ''), (SELECT NULLIF(NULLIF(p.teams, '[]'), '')
                            FROM programs p WHERE p.guid = recordings.program_guid))
       WHERE program_guid IS NOT NULL
         AND (NULLIF(summary, '') IS NULL OR NULLIF(content_rating, '') IS NULL
              OR NULLIF(teams, '') IS NULL)
         AND EXISTS (SELECT 1 FROM programs p WHERE p.guid = recordings.program_guid)""",
)


_GRABS = """UPDATE our_grabs SET teams = (SELECT NULLIF(p.teams, '[]') FROM programs p
           WHERE p.guid = our_grabs.program_guid)
         WHERE teams IS NULL AND program_guid IS NOT NULL
           AND EXISTS (SELECT 1 FROM programs p WHERE p.guid = our_grabs.program_guid
                       AND p.teams IS NOT NULL AND p.teams != '[]')"""

_GRABS_NAME = """UPDATE our_grabs SET
         show_title = COALESCE(show_title, (SELECT NULLIF(p.grandparent_title, '')
                               FROM programs p WHERE p.guid = our_grabs.program_guid)),
         section = COALESCE(section, (SELECT NULLIF(p.section, '')
                            FROM programs p WHERE p.guid = our_grabs.program_guid))
       WHERE (show_title IS NULL OR section IS NULL) AND program_guid IS NOT NULL
         AND EXISTS (SELECT 1 FROM programs p WHERE p.guid = our_grabs.program_guid)"""

_LOG = """UPDATE pass_actions SET parent_title = (SELECT NULLIF(p.grandparent_title, '')
           FROM programs p WHERE p.guid = pass_actions.program_guid)
         WHERE parent_title IS NULL AND program_guid IS NOT NULL
           AND EXISTS (SELECT 1 FROM programs p WHERE p.guid = pass_actions.program_guid)"""


def migrate(conn) -> None:
    """Copy what the guide still has onto every recording. Runs at start, so
    a row made before the columns existed keeps what it can."""
    # Whatever the guide offers goes through the same check as a fetched one.
    conn.create_function("ce_public", 1, _public)
    for sql in (*_REFRESH, _LOG, _GRABS, _GRABS_NAME):
        conn.execute(sql)


def refresh_from_guide() -> None:
    with db.tx() as c:
        migrate(c)


# ---------- fetching it again ----------

def _public(url) -> str:
    """An address a client may be handed: absolute, https, and carrying no
    credential. Anything else is dropped, never kept."""
    u = (url or "").strip()
    if len(u) > MAX_URL or not u.startswith(("https://", "http://")):
        return ""
    low = u.lower()
    if "token" in low or "x-plex" in low or "@" in u.split("//", 1)[1].split("/", 1)[0]:
        return ""
    return u


def discover(guid: str, token: str) -> dict:
    """Plex's own metadata for a plex:// guid: public picture addresses."""
    ident = (guid or "").rsplit("/", 1)[-1]
    import json
    with httpx.stream("GET", f"{DISCOVER_URL}/library/metadata/{ident}", timeout=FETCH_TIMEOUT,
                      headers={"Accept": "application/json", "X-Plex-Token": token}) as r:
        r.raise_for_status()
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > MAX_BODY:
                raise ValueError("answer too large")
    items = json.loads(body).get("MediaContainer", {}).get("Metadata", []) or []
    m = items[0] if items else {}
    return {"art": m.get("art") or m.get("grandparentArt"),
            "thumb": m.get("thumb") or m.get("grandparentThumb")}


def _found(art, thumb) -> tuple[str, str]:
    return _public(art), _public(thumb)


def fetch_art(rec, plex) -> bool:
    """Try every public source for one recording. True if a picture was kept."""
    art = thumb = ""
    token = db.get_setting("plex_token") or ""
    guid = rec["program_guid"] or ""
    if guid.startswith("plex://") and token:
        try:
            got = discover(guid, token)
            art, thumb = _found(got.get("art"), got.get("thumb"))
        except Exception as e:
            log.info("no picture from Plex's catalogue for %s: %s", guid, e)
    if not (art or thumb) and plex is not None and rec["plex_rating_key"]:
        try:
            m = plex.metadata("library", rec["plex_rating_key"]) or {}
            art, thumb = _found(m.get("art") or m.get("grandparentArt"),
                                m.get("thumb") or m.get("grandparentThumb"))
        except Exception as e:
            log.info("no picture from the library for %s: %s", rec["plex_rating_key"], e)
    now = int(time.time())
    if art or thumb:
        with db.tx() as c:
            c.execute("UPDATE recordings SET art = COALESCE(NULLIF(?, ''), art), "
                      "thumb = COALESCE(NULLIF(?, ''), thumb), art_tried_at = ?, "
                      "art_fails = 0 WHERE id = ?", (art, thumb, now, rec["id"]))
        return True
    return False


def _fail(rec_id, now) -> None:
    with db.tx() as c:
        c.execute("UPDATE recordings SET art_tried_at = ?, "
                  "art_fails = COALESCE(art_fails, 0) + 1 WHERE id = ?", (now, rec_id))


def _wait(fails) -> int:
    """An hour, then four, then sixteen, up to a week."""
    return min(7 * DAY, 3600 * 4 ** max(0, (fails or 1) - 1))


def _missing_art(limit=None, now=0):
    """Recordings with no picture whose last miss is old enough to ask again."""
    rows = db.query(
        f"SELECT * FROM recordings WHERE kind = 'recording' AND state IN {SHOWN} "
        "AND COALESCE(art, '') = '' AND COALESCE(thumb, '') = '' "
        "AND (COALESCE(program_guid, '') != '' OR COALESCE(plex_rating_key, '') != '') "
        "ORDER BY id DESC")
    return _due(rows, limit, now)


def _due(rows, limit, now):
    out = []
    for r in rows:
        if r["art_tried_at"] and now < r["art_tried_at"] + _wait(r["art_fails"]):
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def backfill(plex, now: int | None = None, limit: int = PER_PASS,
             deadline: float | None = None) -> int:
    """Fetch the picture of a few recordings that have none. Failures are
    remembered, so a recording Plex has no picture for is not asked every
    minute, and a pass stops starting fetches once its time is used."""
    now = now or int(time.time())
    got = 0
    for rec in _missing_art(limit, now):
        if deadline and time.monotonic() > deadline:
            break
        if fetch_art(rec, plex):
            got += 1
        else:
            _fail(rec["id"], now)
    return got


def _repair_identity(rec, plex) -> bool:
    """A recording with no title: ask the library for what it is."""
    if plex is None or not rec["plex_rating_key"]:
        return False
    try:
        m = plex.metadata("library", rec["plex_rating_key"]) or {}
    except Exception as e:
        log.info("could not read %s from the library: %s", rec["plex_rating_key"], e)
        return False
    if not m.get("title"):
        return False
    with db.tx() as c:
        c.execute("""UPDATE recordings SET title = ?, show_title = COALESCE(show_title, ?),
                     summary = COALESCE(NULLIF(summary, ''), ?),
                     season = COALESCE(season, ?), episode = COALESCE(episode, ?),
                     year = COALESCE(year, ?) WHERE id = ?""",
                  (m["title"], m.get("grandparentTitle"), m.get("summary"),
                   m.get("parentIndex"), m.get("index"), m.get("year"), rec["id"]))
    return True


def _nameless():
    return db.query(
        f"SELECT * FROM recordings WHERE kind = 'recording' AND state IN {SHOWN} "
        "AND COALESCE(title, '') = '' AND COALESCE(show_title, '') = ''")


def daily_check(plex, now: int | None = None, deadline: float | None = None) -> None:
    """Every finished recording has a title. What is missing is repaired from
    Plex, a few at a time and never faster than the backoff allows, and a
    notice is raised only for one that could not be. A missing picture is not
    a fault: a title card stands in and the picture is looked for in the
    background."""
    now = now or int(time.time())
    refresh_from_guide()
    for rec in _due(_nameless(), DAILY_CAP, now):
        if deadline and time.monotonic() > deadline:
            break
        if not _repair_identity(rec, plex):
            _fail(rec["id"], now)
    backfill(plex, now, DAILY_CAP, deadline)
    stuck = _nameless()
    notes = []
    if stuck:
        notes.append({
            "code": health.RECORDING_ART, "severity": "warn",
            "title": "A recording could not be given its details",
            "detail": (f"{len(stuck)} recording(s) have no title and Plex could not supply "
                       "one: " + ", ".join(f"recording {r['id']}" for r in stuck[:5]) + "."),
            "hint": "It is looked for again each day.",
        })
    health.record(notes, now, owns=health.RECORDING_ART_CODES)


def tick(now: int | None = None) -> None:
    """Every minute: copy from the guide, fetch a few, and once a day check.
    Bounded in time: it gives up starting new fetches after PASS_SECONDS."""
    now = now or int(time.time())
    deadline = time.monotonic() + PASS_SECONDS
    refresh_from_guide()
    plex = None
    url, token = db.get_setting("plex_url"), db.get_setting("plex_token")
    if url and token:
        from .plex import Plex
        plex = Plex(url, token, timeout=httpx.Timeout(FETCH_TIMEOUT, connect=5.0))
    try:
        backfill(plex, now, deadline=deadline)
        last = int(db.get_setting("recart_checked_at") or 0)
        if now - last >= DAY:
            db.set_setting("recart_checked_at", now)
            daily_check(plex, now, deadline)
    finally:
        if plex is not None:
            plex.close()


# ---------- its own thread ----------

_stop = threading.Event()
_thread: threading.Thread | None = None


def _run(interval) -> None:
    # Apart from every other loop on purpose: a slow Plex or a hung fetch here
    # must never hold up overtime, resume points or a recording.
    _stop.wait(20)
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:
            log.exception("recording art: %s", e)
        _stop.wait(interval)


def start(interval: float = 60, first_wait: bool = True) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(interval,), daemon=True, name="ce-recart")
    _thread.start()


def stop() -> None:
    _stop.set()


def art_url(base: str, key: str) -> str:
    """Where a client asks for a recording's picture, with its own key."""
    return f"{base}/api/tv/art/{key}"


# ---------- the title card ----------

def _flat_png(w=16, h=9, rgb=(20, 23, 28)) -> bytes:
    """A plain card, for a machine with no Pillow."""
    import struct

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@functools.lru_cache(maxsize=256)
def render(title: str, subtitle: str = "") -> bytes:
    """The programme's name on the CouchElephant background, 16:9."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _flat_png()
    w, h = 1280, 720
    img = Image.new("RGB", (w, h), (20, 23, 28))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 16, h], fill=(232, 163, 61))

    def wrap(text, font, width):
        lines, cur = [], ""
        for word in (text or "").split():
            t = (cur + " " + word).strip()
            if d.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = word
        return lines + ([cur] if cur else [])

    big = ImageFont.load_default(size=84)
    small = ImageFont.load_default(size=46)
    lines = wrap(title, big, w - 160)[:3]
    subs = wrap(subtitle, small, w - 160)[:2]
    block = len(lines) * 100 + (30 + len(subs) * 58 if subs else 0)
    y = (h - block) // 2
    for ln in lines:
        d.text((80, y), ln, font=big, fill=(240, 242, 245))
        y += 100
    y += 30
    for ln in subs:
        d.text((80, y), ln, font=small, fill=(160, 168, 178))
        y += 58
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
