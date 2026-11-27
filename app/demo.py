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
import hmac
import os
import re
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import db, exposure, ratelimit

ENV = "COUCHELEPHANT_DEMO"
REVIEW_USER = "reviewer"
# Paths the open gates let through. The handlers answer 404 when demo is off.
OPEN_PATHS = ("/demo", "/demo/pair", "/healthz")
# A pairing code nobody approved is forgotten after this long (tv.PAIR_SECONDS).
PAIR_SECONDS = 600

router = APIRouter()


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
    r"/api/tv/pair", r"/api/tv/pair/poll",
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
_NO_READ = ("/settings", "/partial/settings", "/api/export", "/api/backups",
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
    key = ratelimit.addr_key(exposure.client_address(request))
    wait = ratelimit.reserve([key])
    if wait:
        resp = _page(ratelimit.say(wait), ok=False, status=429)
        resp.headers["Retry-After"] = str(int(wait) + 1)
        return resp
    want = code()
    if not want or not hmac.compare_digest(review_code.strip().encode(), want.encode()):
        return _page("That review code is not right.", ok=False, status=403)
    # The right review code is not a guess, so a mistyped app code costs nothing.
    ratelimit.refund(key)
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


# ---------- reports ----------

REPORT_NOTE = ("This is a demo, so your report was not sent anywhere. "
               "Thank you for trying it.")
