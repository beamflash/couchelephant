"""FastAPI app: middleware, lifecycle, and the route modules in `routes/`."""
import asyncio
import logging
import os
import re
import threading
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (
    acme_tls,
    adskip,
    announce,
    auth,
    backingstore,
    backups,
    db,
    demo,
    exposure,
    folders,
    handover,
    logring,
    notify,
    overtime,
    passes,
    paths,
    recart,
    recorder,
    reportsend,
    sync,
    tunerbox,
    updates,
)
from .plex import Plex
from .routes import (
    account,
    art,
    data,
    guide,
    notices,
    record,
    report,
    settings,
    tv,
    tv_settings,
)
from .routes import passes as pass_routes
from .routes import update as update_routes
from .routes._shared import ASSET_V, BASE, VERSION, current_user  # noqa: F401
from .routes.account import _configured

log = logging.getLogger("couchelephant")
logring.install()


class _HidePass(logging.Filter):
    """The access log must not hold a video pass: it is a 24 hour credential
    for one recording. /hlsk/<pass>/... is written as /hlsk/-/..."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (hide_pass(args[2]),) + args[3:]
        return True


def hide_pass(text: str) -> str:
    return re.sub(r"/hlsk/[^/\s]+/", "/hlsk/-/", text)


logging.getLogger("uvicorn.access").addFilter(_HidePass())

app = FastAPI(title="CouchElephant", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")

# The three /api/tv ones are how a television finds out it has to pair, and
# pairs. They hand out nothing until an administrator approves the code.
_OPEN_EXACT = frozenset(("/login", "/setup", "/logout", "/healthz", "/welcome",
                         "/favicon.ico", "/api/tv/hello", "/api/tv/pair",
                         "/api/tv/pair/poll"))
_OPEN_PREFIX = ("/static/",)


# What a signed-in non-administrator may not change: the Plex connection,
# accounts, artwork, and anything that copies or replaces the database. They
# keep the guide, the schedule and the passes, which is what the app is for.
_ADMIN_PREFIX = ("/settings", "/api/backups", "/api/backingstore", "/api/import",
                 "/api/export", "/api/tv/devices", "/api/update/apply",
                 "/api/connect.svg", "/api/away")


# The issue-report reads that carry this server's own state. Sending a
# report and its consent are open to any signed-in person, so not here.
_REPORT_PARTS = ("/api/report/server", "/api/report/email")


def _device_may(path):
    """What a paired device may reach with its key: the lists and the video,
    and the two issue-report reads (server part, account email). Never the
    device list itself, or it could approve its friends."""
    if path.startswith("/api/tv/devices"):
        return False
    if path in _REPORT_PARTS:
        return True
    # The channel logos too: a card with no picture draws the channel instead.
    return (path.startswith("/api/tv/") or path.startswith("/hls/")
            or path.startswith("/logo/"))


# Starlette builds request.url from the Host header, so a Host of "x/login?"
# makes request.url.path read "/login" while the router serves what was really
# asked for. Every decision here reads the path the router uses.

def _admin_only(path, method):
    if path.startswith("/api/theme"):
        return False
    if path in ("/settings", "/partial/settings") and method == "GET":
        return False          # reading settings is fine; changing them is not
    if path.startswith("/api/tv/settings"):
        return method != "GET"
    if path == "/api/recorder/library-suggest":
        return True           # it lists folders on this computer
    if path in _REPORT_PARTS:
        return True           # the server's log and settings, scrubbed
    return path.startswith(_ADMIN_PREFIX)


def _is_open(path):
    """A prefix match let /loginanything past the gate. Match the path itself."""
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX) or demo.is_open(path)


@app.middleware("http")
async def _same_origin(request: Request, call_next):
    """Refuse a state-changing request that came from another site.

    With sign-in off, which is the default, nothing else stands between a page
    the user happens to be visiting and this app. That page can POST to
    http://<lan-ip>:8710/settings from their browser, and with no cookie
    required there is nothing to withhold. One such request could null the Plex
    address.

    A browser always sends Origin on a cross-site POST, so comparing it to the
    host this request arrived on is enough, and needs no tokens or session. A
    request with neither header is not from a browser form, so it passes: that
    is curl, and curl was never the threat.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        try:
            sent = urllib.parse.urlsplit(origin).netloc.lower()
        except Exception:
            sent = ""
        here = (request.headers.get("host") or "").lower()
        # "null" and anything that does not parse are cross-site too: a
        # sandboxed page sends Origin: null.
        if not sent or not here or sent != here:
            return JSONResponse(
                {"ok": False,
                 "error": "That request came from another site and was refused."},
                status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _first_run(request: Request, call_next):
    """Nothing works before there is a server to talk to, so say so once,
    on a screen that asks for it, rather than a banner on every page."""
    path = request.scope["path"]
    if (not _is_open(path) and not path.startswith("/settings")
            and not path.startswith("/api/") and not path.startswith("/partial/")
            and request.method == "GET" and not _configured()):
        return RedirectResponse("/welcome", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.scope["path"]
    if auth.mode() == "none" or _is_open(path):
        return await call_next(request)
    if _device_may(path) and tv.device_for(request):
        return await call_next(request)
    # A video address with its own pass, for a TV that fetches it by itself.
    if path.startswith("/hlsk/") and tv.stream_path_ok(path):
        return await call_next(request)
    user = current_user(request)
    if user:
        if _admin_only(path, request.method) and user.get("role") != "admin":
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "administrators only"},
                                    status_code=403)
            return HTMLResponse("Administrators only.", status_code=403)
        return await call_next(request)
    # A fresh switch to local sign-in has no accounts yet, so send the first
    # visitor to create one rather than to a login they cannot pass.
    where = "/setup" if auth.needs_setup() else "/login"
    if path.startswith("/api/") or path.startswith("/partial/"):
        return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
    return RedirectResponse(where, status_code=303)


# Away from home. With the switch on, someone outside the home network gets
# the sign-in page, pairing, and video links that carry their own pass. Not
# the setup or settings pages, not even signed in, and not the API docs.
_OUT_EXACT = frozenset(("/login", "/favicon.ico", "/api/tv/hello"))
_PAIR_PATHS = ("/api/tv/pair", "/api/tv/pair/poll")


def pairing_open_to_outsiders(request) -> bool:
    """First pairing happens at home, unless the connection is HTTPS to the
    domain whose real certificate this server holds. Then a device can check
    who it is talking to with the system's own trust. The name is the Host
    header: uvicorn's ASGI scope carries no TLS transport, so the SNI is out
    of reach without patching its protocol classes, and a client picks both."""
    if demo.enabled():
        return True             # the demo's own sign-in decides (app/demo.py)
    host = (request.headers.get("host") or "").split(":")[0]
    return request.url.scheme == "https" and acme_tls.trusted_for(host)


# Admin work stays home. What the apps call (the /api/tv lists, pairing,
# /hls video) is not here.
_OUT_NEVER = ("/setup", "/welcome", "/settings", "/partial/settings", "/api/away",
              "/docs", "/openapi.json", "/redoc",
              "/api/import", "/api/export", "/api/backups", "/api/backingstore",
              "/api/update/apply", "/api/tv/devices", "/api/connect.svg",
              "/api/recorder/library-suggest")


def _out_never(path):
    return any(path == p or path.startswith(p + "/") or
               (p == "/settings" and path.startswith(p)) for p in _OUT_NEVER)


def _not_found(request):
    """What the away gate answers for what is home only. The public demo says
    "sign in required" instead: nothing there is hidden, only locked."""
    if demo.enabled():
        return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
    return PlainTextResponse("Not found", status_code=404)


@app.middleware("http")
async def _away_gate(request: Request, call_next):
    """Registered after the sign-in gate, so it runs before it."""
    path = request.scope["path"]
    if not exposure.away_on() or exposure.is_home(request):
        return await call_next(request)
    if _out_never(path):
        return PlainTextResponse("Not found", status_code=404)
    # A request with no Host has no business here (HTTP/1.0, a probe), and
    # the redirect to HTTPS has nothing to go on.
    if not (request.headers.get("host") or "").strip():
        return PlainTextResponse("Bad request", status_code=400)
    if path in _PAIR_PATHS:
        if pairing_open_to_outsiders(request):
            return await call_next(request)
        return JSONResponse({"ok": False, "error": "Pair this device at home first."},
                            status_code=403)
    if path in _OUT_EXACT or path.startswith(_OPEN_PREFIX) or demo.is_open(path):
        return await call_next(request)
    if _device_may(path) and tv.device_for(request):
        return await call_next(request)
    if path.startswith("/hlsk/") and tv.stream_path_ok(path):
        return await call_next(request)
    # TV settings change the Plex connection: a paired device with its
    # key, or at home. A signed-in person outside has Settings at home.
    if path.startswith("/api/tv/settings"):
        return _not_found(request)
    # Same for the report's server reads: a paired device from away, or home.
    if path in _REPORT_PARTS:
        return _not_found(request)
    if current_user(request):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/partial/") or auth.mode() == "none":
        return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Demo mode only (app/demo.py): refuses what a visitor to the public demo must
# not do. It runs before the gates above.
app.middleware("http")(demo.lock)


# What the pages need and no more: their own scripts and styles (some are
# inline), pictures from here, from data: and from blob:, video from blob:.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _secure(resp, request):
    h = resp.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "SAMEORIGIN"   # the player frames the guide
    h["Referrer-Policy"] = "same-origin"
    h["Content-Security-Policy"] = CSP
    if request.url.scheme == "https":
        h["Strict-Transport-Security"] = "max-age=31536000"
    if "server" in h:
        del h["server"]
    return resp


_BAD_HOST = re.compile(r"[/\\?#@\s]")


@app.middleware("http")
async def _headers(request: Request, call_next):
    """Outermost, so every answer carries them, a refusal included. A Host
    header that is not a host is refused here, before anything reads it."""
    if _BAD_HOST.search(request.headers.get("host") or ""):
        return _secure(PlainTextResponse("Bad request", status_code=400), request)
    return _secure(await call_next(request), request)


# Outermost: the scheme a trusted proxy reports, before anything reads it.
app.add_middleware(demo.ProxyScheme)


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    """The last layer builds a 500 outside the middleware above, so it gets
    the headers here."""
    log.exception("unhandled error on %s", request.scope.get("path"))
    return _secure(PlainTextResponse("Internal Server Error", status_code=500), request)


# ---------- lifecycle ----------

@app.on_event("startup")
async def startup():
    db.init()
    notify.add_folder_event()
    notify.add_art_event()
    # A test drives sync itself and asserts on the result. A loop waking up
    # underneath it rewrites the database mid-assertion, which is a flake that
    # takes an afternoon to explain.
    backups.init()
    if os.environ.get("COUCHELEPHANT_NO_SYNC_LOOP") == "1":
        return
    # Here, past the test early-return, so a test never announces by accident.
    await asyncio.to_thread(announce.start, paths.port())
    asyncio.create_task(announce_loop())
    asyncio.create_task(sync_loop())
    asyncio.create_task(backup_loop())
    asyncio.create_task(updates.update_loop())
    asyncio.create_task(backingstore_loop())
    asyncio.create_task(recorder_loop())
    asyncio.create_task(outside_loop())
    recart.start()
    # Reports still waiting from last time get their retry thread back.
    await asyncio.to_thread(reportsend.start_flusher)


@app.on_event("shutdown")
async def shutdown():
    await asyncio.to_thread(announce.stop)
    recorder.shutdown()
    recart.stop()
    adskip.shutdown()


def _find_tuners():
    """Ask Plex and the network which HDHomeRun boxes there are."""
    if db.get_setting("plex_url") and db.get_setting("plex_token"):
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            tunerbox.discover(plex)
    else:
        tunerbox.discover()


async def recorder_loop():
    """Start and stop our own recordings. Every few seconds, because a
    recording that starts a minute late has missed the kickoff."""
    await asyncio.sleep(2)
    looked = 0.0
    while True:
        try:
            if (recorder.available() and time.time() - looked > 600
                    and not any(t.name == "tuner-discovery" for t in threading.enumerate())):
                looked = time.time()
                # Its own thread: a slow box must not delay a recording.
                threading.Thread(target=_find_tuners, daemon=True,
                                 name="tuner-discovery").start()
            await asyncio.to_thread(recorder.tick)
            if recorder.available():
                await asyncio.to_thread(handover.tick)
                await asyncio.to_thread(handover.tidy)
                await asyncio.to_thread(folders.tick)
                await asyncio.to_thread(adskip.tick)
        except Exception as e:
            # One bad pass must not be the end of every recording after it,
            # and it must not pass unseen either.
            log.exception("recorder loop: %s", e)
        await asyncio.sleep(5)


async def outside_loop():
    """Once a minute, the work that waits on another server: sports overtime
    (a scoreboard) and resume points (Plex). Apart from the recorder's loop,
    because a server slow to answer must never hold a recording up."""
    await asyncio.sleep(20)
    while True:
        for name, job in (("overtime", overtime.tick), ("resume", handover.send_positions)):
            try:
                if recorder.available():
                    await asyncio.to_thread(job)
            except Exception as e:
                log.exception("%s: %s", name, e)
        await asyncio.sleep(60)


async def sync_loop():
    """Periodic guide refresh plus pass evaluation."""
    await asyncio.sleep(3)
    while True:
        try:
            minutes = int(db.get_setting("sync_minutes") or 60)
        except ValueError:
            minutes = 60
        if db.get_setting("plex_url") and db.get_setting("plex_token"):
            ok, detail = await asyncio.to_thread(sync.full_sync)
            if ok:
                await asyncio.to_thread(passes.run_passes)
        await asyncio.sleep(max(5, minutes) * 60)


def _announce_tick():
    """One pass. Each step has its own try, so a failed announce never stops
    the daily certificate renewal and the other way round."""
    try:
        announce.check()
    except Exception:
        log.exception("the announce check failed")
    try:
        acme_tls.renew_daily()
    except Exception:
        log.exception("the certificate renewal check failed")


async def announce_loop():
    """Notice a new address or a machine rename, and retry a failed announce."""
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(_announce_tick)


async def backup_loop():
    """Run each backup job when its own interval says so."""
    await asyncio.sleep(20)
    while True:
        try:
            now = int(time.time())
            for job in await asyncio.to_thread(backups.jobs):
                if not job["enabled"] or not job["every_hours"]:
                    continue
                due = (job["last_run"] or 0) + job["every_hours"] * 3600
                if now >= due:
                    await asyncio.to_thread(backups.run_job, job["id"], VERSION)
        except Exception:
            # A backup that fails is recorded on the job. The loop must not
            # be the thing that stops.
            pass
        await asyncio.sleep(300)


async def backingstore_loop():
    """Reconcile with the backing store on its own timer.

    It also runs shortly after startup, so a machine that was off picks up
    what changed elsewhere without anybody pressing anything.
    """
    await asyncio.sleep(45)
    while True:
        try:
            minutes = int(db.get_setting("backingstore_auto_minutes") or 0)
        except ValueError:
            minutes = 0
        if minutes and backingstore.chosen() is not None:
            try:
                await asyncio.to_thread(backingstore.sync_all)
            except Exception as e:
                backingstore._status(ok=False, at=int(time.time()),
                                     detail=f"{type(e).__name__}: {e}")
        await asyncio.sleep(max(5, minutes or 30) * 60)



@app.get("/healthz")
def healthz():
    if demo.enabled():
        return JSONResponse({"ok": True})      # nothing about the demo is shown unsigned
    last = db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
    return JSONResponse({
        "ok": True,
        "configured": bool(db.get_setting("plex_url") and db.get_setting("plex_token")),
        "dry_run": db.get_setting("dry_run") == "1",
        "last_sync": dict(last) if last else None,
        "airings": db.one("SELECT COUNT(*) n FROM airings")["n"],
        "passes": db.one("SELECT COUNT(*) n FROM passes")["n"],
        "logos": sync.logo_coverage(),
    })


for r in (demo, account, art, data, guide, notices, record, report, pass_routes, settings, tv, tv_settings,
          update_routes):
    app.include_router(r.router)
