"""A recording's picture, for a client that holds the key.

Behind the same sign-in as every other /api/tv call. It answers with, in
order: the public picture the recording kept (as a redirect), the Plex
library's own picture fetched here with the token in a header (the token never
leaves this server), or a drawn title card, so there is always an image.
"""
import threading
import time
from collections import OrderedDict

import httpx
from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

from .. import db, recart

router = APIRouter()

MAX_IMAGE = 5 * 1024 * 1024
CACHE_ENTRIES = 64
MISS_SECONDS = 600                # a library picture that failed is not asked again for this long
_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_missed: dict[str, float] = {}
_lock = threading.Lock()


def _words(key):
    """The name to draw for a card key, from the recording that owns it."""
    rec = db.one("SELECT title, show_title FROM recordings WHERE uid = ?", (key,))
    if rec:
        big = rec["show_title"] or rec["title"] or "Recording"
        return big, (rec["title"] if rec["show_title"] and rec["title"] != rec["show_title"]
                     else "")
    if key.startswith("plex-"):
        g = db.one("SELECT title, parent_title FROM plex_grabs WHERE id = ?", (key[5:],))
        if g:
            return (g["parent_title"] or g["title"] or "Recording",
                    g["title"] if g["parent_title"] else "")
    return None


def _library_picture(rec):
    """The library item's picture, fetched with the token in a header."""
    key = rec["plex_rating_key"]
    base, token = db.get_setting("plex_url"), db.get_setting("plex_token")
    if not (key and base and token):
        return None
    with _lock:
        if _missed.get(key, 0) > time.time():
            return None
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    got = None
    try:
        h = {"X-Plex-Token": token}
        meta = httpx.get(f"{base.rstrip('/')}/library/metadata/{key}", timeout=10,
                         headers={**h, "Accept": "application/json"})
        meta.raise_for_status()
        item = (meta.json().get("MediaContainer", {}).get("Metadata") or [{}])[0]
        path = item.get("thumb") or item.get("grandparentThumb") or item.get("art") or ""
        if path.startswith("/"):                 # only this server's own paths
            with httpx.stream("GET", base.rstrip("/") + path, timeout=10, headers=h) as r:
                r.raise_for_status()
                kind = r.headers.get("content-type", "image/jpeg").split(";")[0]
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_IMAGE:
                        raise ValueError("picture too large")
            if kind.startswith("image/") and body:
                got = (body, kind)
    except Exception:
        got = None
    with _lock:
        if got:
            _cache[key] = got
            while len(_cache) > CACHE_ENTRIES:
                _cache.popitem(last=False)
        else:
            _missed[key] = time.time() + MISS_SECONDS
    return got


@router.get("/api/tv/art/{key}")
def art(key: str):
    rec = db.one("SELECT * FROM recordings WHERE uid = ?", (key,))
    if rec:
        kept = recart._public(rec["art"] or rec["thumb"])
        if kept:
            return RedirectResponse(kept, status_code=302)
        pic = _library_picture(rec)
        if pic:
            return Response(pic[0], media_type=pic[1],
                            headers={"Cache-Control": "private, max-age=3600"})
    words = _words(key)
    if not words:
        return Response(status_code=404)
    return Response(recart.render(*words), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})
