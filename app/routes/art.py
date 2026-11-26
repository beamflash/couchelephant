"""A recording's picture, for a client that holds the key.

Behind the same sign-in as every other /api/tv call. It answers with an image
and never a redirect (the apps refuse to follow one to another host): the kept
public picture fetched here, or the Plex library's own picture fetched here with
the token in a header (the token never leaves this server), or a drawn title
card, so there is always an image.

Fetches are bounded: one upstream fetch per picture however many ask, a few
at a time overall, a short wait for anyone who has to share one, a size cap, an
allow-list of image types, and a cache with a byte budget.
"""
import logging
import threading
import time
from collections import OrderedDict

import httpx
from fastapi import APIRouter
from fastapi.responses import Response

from .. import db, recart

router = APIRouter()
log = logging.getLogger("couchelephant.art")

MAX_IMAGE = 5 * 1024 * 1024
CACHE_BYTES = 64 * 1024 * 1024
MISS_SECONDS = 600           # a picture that failed is not asked for again for this long
MAX_MISSES = 1024
DOWNLOAD_SECONDS = 15.0      # most one picture download may take, start to end
WAIT = 8.0                   # most a caller waits on someone else's fetch, or for a slot
ALLOWED = ("image/jpeg", "image/png", "image/webp")      # never svg: it can carry script
HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
# A title card stands in until the real picture is found, so it is not kept long.
CARD_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"}

_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_bytes = [0]
_missed: dict[str, float] = {}
_inflight: dict[str, threading.Event] = {}
_lock = threading.Lock()
_gate = threading.Semaphore(4)


def _reset() -> None:
    with _lock:
        _cache.clear()
        _missed.clear()
        _inflight.clear()
        _bytes[0] = 0


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


def _store(key, got) -> None:
    """Keep a result, or remember a miss. Called with the lock held."""
    now = time.time()
    if got is None:
        for k in [k for k, t in _missed.items() if t <= now]:
            del _missed[k]
        while len(_missed) >= MAX_MISSES:
            _missed.pop(next(iter(_missed)))
        _missed[key] = now + MISS_SECONDS
        return
    if len(got[0]) > CACHE_BYTES:
        return
    if key in _cache:
        _bytes[0] -= len(_cache.pop(key)[0])
    _cache[key] = got
    _bytes[0] += len(got[0])
    while _bytes[0] > CACHE_BYTES and _cache:
        _bytes[0] -= len(_cache.popitem(last=False)[1][0])


def _get(key, fetch):
    """The picture for `key`, from the cache, or by `fetch()`, once for
    everyone asking. None means there is none to be had right now."""
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        if _missed.get(key, 0) > time.time():
            return None
        waiting = _inflight.get(key)
        if waiting is None:
            _inflight[key] = done = threading.Event()
    if waiting is not None:                      # someone is already fetching it
        waiting.wait(WAIT)
        with _lock:
            return _cache.get(key)
    got, tried = None, False
    try:
        if _gate.acquire(timeout=WAIT):
            tried = True
            try:
                got = fetch()
            finally:
                _gate.release()
    except Exception as e:
        log.info("picture fetch failed: %s", type(e).__name__)
        got = None
    finally:
        with _lock:
            if tried:
                _store(key, got)
            _inflight.pop(key, None)
        done.set()
    return got


def _download(url, headers=None):
    """One image, never following a redirect, never past the cap, never of a
    type that is not on the list. A missing type is a refusal."""
    # httpx's timeout restarts on every read, so a server that drips a byte
    # at a time would hold the slot for hours: a whole-download deadline too.
    # Identity encoding, so a small gzip body cannot unpack past the cap.
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    with httpx.stream("GET", url, timeout=10,
                      headers={**(headers or {}), "Accept-Encoding": "identity"}) as r:
        r.raise_for_status()
        kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if kind not in ALLOWED:
            return None
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_IMAGE:
            return None
        body = bytearray()
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > MAX_IMAGE or time.monotonic() > deadline:
                return None
    return (bytes(body), kind) if body else None


def _library_picture(rec):
    key = rec["plex_rating_key"]
    base, token = db.get_setting("plex_url"), db.get_setting("plex_token")
    if not (key and base and token):
        return None
    base = base.rstrip("/")

    def fetch():
        h = {"X-Plex-Token": token}
        meta = httpx.get(f"{base}/library/metadata/{key}", timeout=10,
                         headers={**h, "Accept": "application/json"})
        meta.raise_for_status()
        item = (meta.json().get("MediaContainer", {}).get("Metadata") or [{}])[0]
        path = item.get("thumb") or item.get("grandparentThumb") or item.get("art") or ""
        # Only this server's own paths, so the token never goes anywhere else.
        return _download(base + path, h) if path.startswith("/") else None
    return _get("lib:" + key, fetch)


def _kept_picture(url):
    return _get("pub:" + url, lambda: _download(url))


@router.get("/api/tv/art/{key}")
def art(key: str):
    rec = db.one("SELECT * FROM recordings WHERE uid = ?", (key,))
    if rec:
        kept = recart.best(rec["art"], rec["thumb"])
        pic = (kept and _kept_picture(kept)) or _library_picture(rec)
        if pic:
            return Response(pic[0], media_type=pic[1], headers=HEADERS)
    words = _words(key)
    if not words:
        return Response(status_code=404)
    return Response(recart.render(*words), media_type="image/png", headers=CARD_HEADERS)
