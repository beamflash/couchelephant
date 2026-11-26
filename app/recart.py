"""What a recording needs to be shown, kept on the recording.

The guide is a cache. It forgets a programme once it has aired, so a recording
that read its picture, description or teams from the guide went blank a few
days after it was made. Each recording now carries its own copy: filled when it
is booked, refreshed while the guide still has the programme, fetched again
from Plex when the guide is gone, and, when no picture exists anywhere, drawn
as a title card so no tile is ever empty.
"""
import functools
import hmac
import io
import logging
import os
import secrets
import time
import zlib

import httpx

from . import db, health

log = logging.getLogger("couchelephant.recart")

DISCOVER_URL = os.environ.get("COUCHELEPHANT_DISCOVER_URL",
                              "https://discover.provider.plex.tv")
# A finished recording: the ones whose picture matters and will not come back.
SHOWN = "('recording', 'finished', 'moving', 'in_plex', 'failed')"
PER_PASS = 2                 # fetches per minute, so Plex is never hammered
DAY = 86400


# ---------- copying from the guide ----------

_REFRESH = (
    # The guide's picture wins while it has one: it is the newest. A blank one
    # never replaces what is kept.
    """UPDATE recordings SET
         art = COALESCE((SELECT NULLIF(p.art, '') FROM programs p
                         WHERE p.guid = recordings.program_guid), art),
         thumb = COALESCE((SELECT NULLIF(p.thumb, '') FROM programs p
                           WHERE p.guid = recordings.program_guid), thumb)
       WHERE program_guid IS NOT NULL AND EXISTS (
         SELECT 1 FROM programs p WHERE p.guid = recordings.program_guid
           AND ((NULLIF(p.art, '') IS NOT NULL
                 AND p.art IS NOT COALESCE(recordings.art, ''))
             OR (NULLIF(p.thumb, '') IS NOT NULL
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


def migrate(conn) -> None:
    """Copy what the guide still has onto every recording. Runs at start, so
    a row made before the columns existed keeps what it can."""
    for sql in _REFRESH:
        conn.execute(sql)


def refresh_from_guide() -> None:
    with db.tx() as c:
        migrate(c)


# ---------- fetching it again ----------

def _public(url) -> str:
    """An address a client may be handed: absolute, https, and carrying no
    credential. Anything else is dropped, never kept."""
    u = (url or "").strip()
    if not u.startswith(("https://", "http://")):
        return ""
    low = u.lower()
    if "token" in low or "x-plex" in low or "@" in u.split("//", 1)[1].split("/", 1)[0]:
        return ""
    return u


def discover(guid: str, token: str) -> dict:
    """Plex's own metadata for a plex:// guid: public picture addresses."""
    ident = (guid or "").rsplit("/", 1)[-1]
    r = httpx.get(f"{DISCOVER_URL}/library/metadata/{ident}", timeout=15,
                  headers={"Accept": "application/json", "X-Plex-Token": token})
    r.raise_for_status()
    items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
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


def _missing_art(limit=None, now=0, forced=False):
    rows = db.query(
        f"SELECT * FROM recordings WHERE kind = 'recording' AND state IN {SHOWN} "
        "AND COALESCE(art, '') = '' AND COALESCE(thumb, '') = '' "
        "AND (COALESCE(program_guid, '') != '' OR COALESCE(plex_rating_key, '') != '') "
        "ORDER BY id DESC")
    out = []
    for r in rows:
        if not forced and r["art_tried_at"] and now < r["art_tried_at"] + _wait(r["art_fails"]):
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def backfill(plex, now: int | None = None, limit: int = PER_PASS) -> int:
    """Fetch the picture of a few recordings that have none. Failures are
    remembered, so a recording Plex has no picture for is not asked every
    minute."""
    now = now or int(time.time())
    got = 0
    for rec in _missing_art(limit, now):
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


def daily_check(plex, now: int | None = None) -> None:
    """Every finished recording has a title and a picture. What is missing is
    repaired from Plex; a notice is raised only for what could not be."""
    now = now or int(time.time())
    refresh_from_guide()
    nameless = db.query(
        f"SELECT * FROM recordings WHERE kind = 'recording' AND state IN {SHOWN} "
        "AND COALESCE(title, '') = '' AND COALESCE(show_title, '') = ''")
    stuck = [r for r in nameless if not _repair_identity(r, plex)]
    for rec in _missing_art(now=now, forced=True):
        if not fetch_art(rec, plex):
            _fail(rec["id"], now)
    faded = db.query(
        f"SELECT * FROM recordings WHERE kind = 'recording' AND state IN {SHOWN} "
        "AND COALESCE(art, '') = '' AND COALESCE(thumb, '') = '' "
        "AND COALESCE(art_fails, 0) >= 3")
    notes = []
    if stuck or faded:
        names = [(r["title"] or r["show_title"] or f"recording {r['id']}")
                 for r in [*stuck, *faded]][:5]
        notes.append({
            "code": health.RECORDING_ART, "severity": "warn",
            "title": "A recording could not be given its details",
            "detail": (f"{len(stuck)} recording(s) have no title and {len(faded)} have no "
                       "picture that Plex could supply: " + ", ".join(names) + "."),
            "hint": "A title card is shown in place of the picture. It is looked for "
                    "again each day.",
        })
    health.record(notes, now, owns=health.RECORDING_ART_CODES)


def tick(now: int | None = None) -> None:
    """Every minute: copy from the guide, fetch a few, and once a day check."""
    now = now or int(time.time())
    refresh_from_guide()
    plex = None
    url, token = db.get_setting("plex_url"), db.get_setting("plex_token")
    if url and token:
        from .plex import Plex
        plex = Plex(url, token)
    try:
        backfill(plex, now)
        last = int(db.get_setting("recart_checked_at") or 0)
        if now - last >= DAY:
            db.set_setting("recart_checked_at", now)
            daily_check(plex, now)
    finally:
        if plex is not None:
            plex.close()


# ---------- the title card ----------

def _secret() -> bytes:
    s = db.get_setting("card_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("card_secret", s)
    return s.encode()


def sign(key: str) -> str:
    return hmac.new(_secret(), key.encode(), "sha256").hexdigest()[:32]


def signature_ok(key: str, sig: str) -> bool:
    return hmac.compare_digest(sign(key), sig or "")


def card_url(base: str, key: str) -> str:
    return f"{base}/card/{key}/{sign(key)}.png"


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
