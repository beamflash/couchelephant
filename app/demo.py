"""Demo mode: the public copy an App Store reviewer signs in to.

Switched on by COUCHELEPHANT_DEMO=1 (scripts/demo_server.py does that, and
also runs the invented guide, the fake tuner and the seeded recordings). All
of this file is policy and does nothing when the switch is off.

What it changes:

  - Pairing. A reviewer cannot be at "home", and with the apps' own away
    rule an outsider cannot approve a code in Settings. So the demo has its
    own small page, /demo: the review code (DEMO_CODE, from the environment)
    plus the six letters the app shows lets that app in.
  - A lock on writes. Only what the apps and the guide need may change
    anything. Settings, accounts, devices, backups, import and the rest
    answer 403, so a visitor cannot break the demo or make it fetch a URL.
  - Reports. They are never sent anywhere. The web page says so politely.
  - The proxy. Behind Caddy the connection is plain HTTP, so the scheme is
    taken from the trusted proxy's X-Forwarded-Proto. Without it the
    server hands out http:// picture links the apps would not send their key to.
"""
import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import db, exposure, ratelimit

ENV = "COUCHELEPHANT_DEMO"
REVIEW_USER = "reviewer"
# Paths the open gates let through. The handlers answer 404 when demo is off.
OPEN_PATHS = ("/demo", "/demo/pair", "/healthz", "/api/tv/pair/code")
# A pairing code nobody approved is forgotten after this long (tv.PAIR_SECONDS).
PAIR_SECONDS = 600

router = APIRouter()

# Set while the nightly reset rebuilds the demo. Everything but /healthz
# answers 503 meanwhile, so nothing is served half made.
RESETTING = threading.Event()


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def code() -> str:
    """The review code. Never written down in the source: it comes from the
    environment, and the demo refuses to start without one."""
    return os.environ.get("DEMO_CODE", "")


def is_open(path: str) -> bool:
    return enabled() and path in OPEN_PATHS


# ---------- the lock on writes ----------

# Everything else that is not a read is refused. Each line is something the
# apps or the web page do while someone looks around.
_MAY_WRITE = tuple(re.compile(p + r"$") for p in (
    r"/login", r"/logout", r"/api/theme", r"/demo/pair",
    r"/api/tv/pair", r"/api/tv/pair/poll", r"/api/tv/pair/code",
    # the web page's own recording and pass screens
    r"/api/record", r"/api/record/cancel", r"/api/pass", r"/api/rules",
    r"/api/rules/\d+", r"/api/channels-preview", r"/api/filter/preview",
    r"/passes/\d+/(delete|toggle)", r"/api/schedule/retry",
    r"/api/announced/follow", r"/api/notices/[a-z0-9_.-]+/dismiss",
    r"/api/report/consent", r"/api/report/send",
    # what the apps do
    r"/api/tv/favorites/(add|remove)", r"/api/tv/record(/all|/cancel|/move)?",
    r"/api/tv/passes/(new|create|\d+|\d+/delete|lost/\d+/(cancel|follow))",
    r"/api/tv/recordings/\d+/position", r"/api/tv/schedule/retry",
    r"/api/tv/live/[\w.]+", r"/api/tv/live/\d+/(stop|keepalive)",
))

# Reads that show the owner's own machine or its settings. A plain reviewer
# has no use for them, and the web pages for them stay shut.
_NO_READ = ("/settings", "/partial/settings", "/api/export", "/api/tv/settings/test", "/api/import", "/api/backups",
            "/api/backingstore", "/api/away", "/api/connect.svg", "/api/tv/devices",
            "/api/recorder/library-suggest", "/api/update", "/welcome", "/setup",
            "/docs", "/openapi.json", "/redoc")


def blocked(method: str, path: str) -> bool:
    """Should demo mode refuse this request outright."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return any(path == p or path.startswith(p + "/") for p in _NO_READ)
    return not any(rx.match(path) for rx in _MAY_WRITE)


NOT_HERE = "This is a demo, so that is switched off here."


async def lock(request: Request, call_next):
    """An http middleware for web.py. The path read is the one the router
    serves, as every other gate in web.py does."""
    if not enabled():
        return await call_next(request)
    path = request.scope["path"]
    if RESETTING.is_set() and path != "/healthz":
        return JSONResponse({"ok": False, "error": "The demo is resetting. Try again in a minute."},
                            status_code=503, headers={"Retry-After": "60"})
    if request.method in ("GET", "HEAD") and (path == "/settings" or path.startswith("/settings/")
                                              or path == "/partial/settings"):
        return RedirectResponse("/demo", status_code=303)
    if blocked(request.method, path):
        if path.startswith(("/api/", "/partial/")) or request.method != "GET":
            return JSONResponse({"ok": False, "error": NOT_HERE}, status_code=403)
        return PlainTextResponse(NOT_HERE, status_code=403)
    if not _carries_credentials(request, path):
        # Nothing at all without the code. The gates behind this check the
        # credential is real; this is the one place that says no to everyone
        # who brought none, the same way for every path.
        if path.startswith(("/api/", "/partial/")) or request.method != "GET":
            return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Open to anyone, and each shows nothing: the sign-in routes, the pairing the
# apps start with, the static files of the sign-in page. A video address
# carries its own signed pass in its path.
_OPEN = frozenset(("/login", "/logout", "/api/tv/hello", "/api/tv/pair", "/api/tv/pair/poll"))


def _carries_credentials(request, path) -> bool:
    return bool(is_open(path) or path in _OPEN or path.startswith(("/static/", "/hlsk/"))
                or request.headers.get("authorization") or request.cookies.get("ce_session"))


# ---------- behind the proxy ----------

class ProxyScheme:
    """Takes http or https from X-Forwarded-Proto, but only from a proxy the
    install was told to trust. Anyone else cannot change what the server
    believes about its own connection."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if enabled() and scope["type"] in ("http", "websocket"):
            peer = exposure._parse((scope.get("client") or ("",))[0])
            if peer is not None and exposure._is_trusted(peer, exposure._trusted()):
                for k, v in scope["headers"]:
                    if k == b"x-forwarded-proto":
                        proto = v.decode("latin-1").split(",")[0].strip().lower()
                        if proto in ("http", "https"):
                            scope = dict(scope, scheme=proto if scope["type"] == "http"
                                         else {"http": "ws", "https": "wss"}[proto])
                        break
        await self.app(scope, receive, send)


# ---------- the review code's own guess budget ----------

# Wrong guesses from one address, in the last WINDOW seconds. An office of
# reviewers on one address may mistype now and then, so the budget is far wider
# than the sign-in limit; the code is long enough that 20 guesses in ten
# minutes find nothing. Past the budget even the right code waits.
GUESS_BUDGET = 20
GUESS_WINDOW = 600
_guesses: dict = {}
_guess_lock = threading.Lock()


# An address that has entered the right code keeps being let in with it for a
# day, even after its wrong-try budget has run out (a mistyping colleague on
# the same address must not lock the reviewer out), but only with that exact
# code and with a looser limit of its own.
GOOD_SECONDS = 24 * 3600
GOOD_BUDGET = 60
_good: dict = {}
_loose: dict = {}


def _digest(typed: str) -> bytes:
    return hashlib.sha256(typed.strip().lower().encode()).digest()


def check_code(addr, typed: str):
    """(ok, wait): is `typed` the review code. A right code always passes while
    the address is inside its budget, whatever it got wrong before. `wait` is
    the seconds to wait when the address is out of budget, else 0."""
    key = ratelimit.addr_key(addr)
    now = time.monotonic()
    want = code().lower()
    right = bool(want) and hmac.compare_digest(typed.strip().lower().encode(), want.encode())
    with _guess_lock:
        fails = [t for t in _guesses.get(key, ()) if now - t < GUESS_WINDOW]
        _guesses[key] = fails
        if len(fails) >= GUESS_BUDGET:
            wait = fails[0] + GUESS_WINDOW - now
            seen = _good.get(key)
            if right and seen and now - seen[0] < GOOD_SECONDS and hmac.compare_digest(
                    seen[1], _digest(typed)):
                loose = [t for t in _loose.get(key, ()) if now - t < GUESS_WINDOW]
                if len(loose) < GOOD_BUDGET:
                    loose.append(now)
                    _loose[key] = loose
                    return True, 0
            return False, wait
        if right:
            _good[key] = (now, _digest(typed))
            if len(_good) > 10000:
                del _good[min(_good, key=lambda k: _good[k][0])]
            return True, 0
        fails.append(now)
        if len(_guesses) > 10000:
            for k in sorted(_guesses, key=lambda k: _guesses[k][-1] if _guesses[k] else 0)[:2000]:
                del _guesses[k]
        return False, 0


def reset_guesses() -> None:
    with _guess_lock:
        _guesses.clear()
        _good.clear()
        _loose.clear()


# ---------- the review sign-in ----------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CouchElephant demo sign-in</title>
<style>
body{font:16px/1.45 -apple-system,system-ui,sans-serif;max-width:30rem;margin:2rem auto;
padding:0 1rem;color:#222;background:#fafafa}
h1{font-size:1.4rem} label{display:block;margin:1rem 0 .25rem;font-weight:600}
input{font:inherit;padding:.6rem;width:100%;box-sizing:border-box;border:1px solid #999;
border-radius:6px}
input.code{font-family:ui-monospace,monospace;letter-spacing:.2em;text-transform:uppercase}
button{font:inherit;margin-top:1.2rem;padding:.7rem 1.2rem;border:0;border-radius:6px;
background:#c2590a;color:#fff;font-weight:600}
.msg{padding:.7rem;border-radius:6px;margin:1rem 0}
.ok{background:#dff3df}.bad{background:#f8dcdc}
@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}
input{background:#222;color:#eee;border-color:#666}.ok{background:#1f3d1f}.bad{background:#4a2020}}
</style></head><body>
<h1>CouchElephant demo</h1>
<p>This is a public demo with invented television. Nothing here is real, and it
resets every night.</p>
<p>To let an app in: open the app, connect it to this address, and type the
six letters it shows below, with the review code from the App Store notes.</p>
@@MESSAGE@@
<form method="post" action="/demo/pair" autocomplete="off">
<label for="rc">Review code</label>
<input id="rc" name="review_code" type="password" autocomplete="off" required>
<label for="c">Code shown in the app</label>
<input id="c" class="code" name="code" maxlength="6" autocapitalize="characters"
 autocomplete="off" required>
<button type="submit">Let the app in</button>
</form></body></html>"""


def _page(message="", ok=True, status=200):
    box = f'<div class="msg {"ok" if ok else "bad"}">{message}</div>' if message else ""
    return HTMLResponse(_PAGE.replace("@@MESSAGE@@", box), status_code=status)


@router.get("/demo", response_class=HTMLResponse)
def demo_page():
    if not enabled():
        return PlainTextResponse("Not found", status_code=404)
    return _page()


@router.post("/demo/pair", response_class=HTMLResponse)
def demo_pair(request: Request, review_code: str = Form(""), code_shown: str = Form("", alias="code")):
    if not enabled():
        return PlainTextResponse("Not found", status_code=404)
    ok, wait = check_code(exposure.client_address(request), review_code)
    if wait:
        resp = _page(ratelimit.say(wait), ok=False, status=429)
        resp.headers["Retry-After"] = str(int(wait) + 1)
        return resp
    if not ok:
        return _page("That review code is not right.", ok=False, status=403)
    shown = code_shown.strip().upper()
    row = db.one("SELECT * FROM tv_devices WHERE code = ? AND approved_at IS NULL "
                 "AND created_at > ?", (shown, int(time.time()) - PAIR_SECONDS))
    if not row:
        return _page("No app is showing that code. Check it, and that the app is still "
                     "waiting (a code lasts ten minutes).", ok=False, status=404)
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET approved_at = ? WHERE id = ?",
                  (int(time.time()), row["id"]))
    return _page("Done. Go back to the app: it comes in within a few seconds.")


# ---------- the access code, typed in the app ----------

# The apps show an "Access code" field when hello says code_signin, and send
# the code here. It is the review code (DEMO_CODE): a right one makes a paired
# device and answers its key, as an approved pairing does. Without the right
# code nothing is made. Not in a normal install: 404 there, and hello never
# says code_signin.
MAX_DEVICES = 200


@router.post("/api/tv/pair/code")
def pair_by_code(request: Request, code_typed: str = Form("", alias="code"),
                 name: str = Form("Apple TV")):
    if not enabled():
        return PlainTextResponse("Not found", status_code=404)
    ok, wait = check_code(exposure.client_address(request), code_typed)
    if wait:
        return JSONResponse({"ok": False, "error": ratelimit.say(wait)}, status_code=429,
                            headers={"Retry-After": str(int(wait) + 1)})
    if not ok:
        return JSONResponse({"ok": False, "error": "That access code is not right."},
                            status_code=403)
    device_key = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.tx() as c:
        # A demo does not collect devices for ever.
        c.execute("DELETE FROM tv_devices WHERE id IN (SELECT id FROM tv_devices "
                  "ORDER BY id DESC LIMIT -1 OFFSET ?)", (MAX_DEVICES - 1,))
        c.execute("INSERT INTO tv_devices (name, key_hash, approved_at, created_at) "
                  "VALUES (?, ?, ?, ?)",
                  (name[:60], hashlib.sha256(device_key.encode()).hexdigest(), now, now))
    return {"ok": True, "key": device_key}


# ---------- limits on what a visitor can fill the disk with ----------

MAX_BOOKINGS = 2                       # things a visitor has set to record at once
MAX_RECORDING_SECONDS = 30 * 60        # a recording they make stops after this
MAX_LIVE_SECONDS = 30 * 60             # a live session, however it is kept alive
MAX_DISK_BYTES = 3 * 1024 ** 3         # the recordings folder, all told
SEED_KEY = "demo_seed_max_id"          # recordings up to this id are the seeded ones
# The demo's own rolling recording (tests/demo_world.keep_one_under_way) is not
# a visitor's: none of the limits below count it.
OWN_SOURCE = "demo-under-way"
_disk_checked = [0.0]


def _seed_max() -> int:
    try:
        return int(db.get_setting(SEED_KEY) or 0)
    except ValueError:
        return 0


def _folder_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def enforce_limits(now: int | None = None) -> None:
    """Called every few seconds by the demo's own loop. Only what a visitor
    made is ever touched: the seeded recordings (ids up to SEED_KEY) never are."""
    from . import recorder
    now = now or int(time.time())
    seed = _seed_max()
    # Live sessions end after half an hour, whatever keeps them alive.
    for rec in db.query("SELECT * FROM recordings WHERE kind = 'live' AND created_at < ?",
                        (now - MAX_LIVE_SECONDS,)):
        recorder.stop_live(rec["id"])
    # At most MAX_BOOKINGS of a visitor's bookings: the earliest stay.
    mine = db.query("SELECT id FROM recordings WHERE kind = 'recording' AND id > ? "
                    "AND COALESCE(source, '') != ? "
                    "AND state IN ('scheduled', 'recording') ORDER BY COALESCE(starts_at, begins_at), id",
                    (seed, OWN_SOURCE))
    for row in mine[MAX_BOOKINGS:]:
        recorder.cancel(row["id"])
    # A recording they make stops after half an hour.
    with db.tx() as c:
        c.execute("UPDATE recordings SET stops_at = COALESCE(started_at, starts_at) + ? "
                  "WHERE kind = 'recording' AND id > ? AND state IN ('scheduled', 'recording') "
                  "AND COALESCE(source, '') != ? "
                  "AND starts_at IS NOT NULL AND stops_at > COALESCE(started_at, starts_at) + ?",
                  (MAX_RECORDING_SECONDS, seed, OWN_SOURCE, MAX_RECORDING_SECONDS))
    # The folder has a ceiling: the oldest finished recording of theirs goes first.
    if time.monotonic() - _disk_checked[0] > 60:
        _disk_checked[0] = time.monotonic()
        # The demo's own recording is not the visitor's to pay for.
        own = sum(_folder_bytes(recorder.rec_dir(r["uid"])) for r in db.query(
            "SELECT uid FROM recordings WHERE source = ?", (OWN_SOURCE,)))
        size = _folder_bytes(recorder.REC_DIR) - own
        if size > MAX_DISK_BYTES:
            for rec in db.query("SELECT id, uid FROM recordings WHERE kind = 'recording' "
                                "AND id > ? AND state = 'finished' ORDER BY id", (seed,)):
                shutil.rmtree(recorder.rec_dir(rec["uid"]), ignore_errors=True)
                with db.tx() as c:
                    c.execute("DELETE FROM ad_breaks WHERE recording_id = ?", (rec["id"],))
                    c.execute("DELETE FROM recordings WHERE id = ?", (rec["id"],))
                size = _folder_bytes(recorder.REC_DIR) - own
                if size <= MAX_DISK_BYTES:
                    break


def recently_used(seconds: int = 1800) -> bool:
    """Has anyone with a key or a session been here lately. The nightly reset
    waits for a quiet half hour, so a reviewer is never cut off mid-look."""
    from . import auth
    since = int(time.time()) - seconds
    seen = db.one("SELECT MAX(MAX(COALESCE(last_seen, 0), COALESCE(approved_at, 0))) AS t "
                  "FROM tv_devices")
    web = auth._con().execute("SELECT MAX(created_at) FROM sessions").fetchone()[0] or 0
    return max((seen["t"] if seen and seen["t"] else 0), web) > since


# ---------- reports ----------

def scrub_tv_settings(values: dict) -> dict:
    """The settings an app's screen reads, without the addresses and keys the
    demo's own machinery uses."""
    from .reportscrub import HOST_SETTINGS, SECRET_SETTINGS_ALL
    hide = set(HOST_SETTINGS) | set(SECRET_SETTINGS_ALL) | {"plex_url", "tuner_url",
                                                          "trusted_proxies", "away_domain"}
    return {k: v for k, v in values.items() if k not in hide}


REPORT_NOTE = ("This is a demo, so your report was not sent anywhere. "
               "Thank you for trying it.")
