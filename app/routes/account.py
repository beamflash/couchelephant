"""First run, sign in, sign out, theme."""
import logging
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, db, exposure, language, ratelimit
from ._shared import ZONES, current_user, page
from .settings import _save_and_sync, _test_plex

router = APIRouter()
log = logging.getLogger("couchelephant")

# ---------- first run ----------

def _configured():
    return bool(db.get_setting("plex_url") and db.get_setting("plex_token"))


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    return page(request, "welcome.html", zones=ZONES, nav="",
                languages=language.LANGUAGES)


@router.post("/welcome")
async def welcome_save(plex_url: str = Form(""), plex_token: str = Form(""),
                       timezone: str = Form("UTC"), preferred_language: str = Form("en")):
    """Test first, then save. A wrong address saved quietly is what makes the
    first five minutes confusing."""
    if _configured():
        return JSONResponse({"ok": False, "detail": "CouchElephant is already set up. "
                             "Change Plex under Settings."}, status_code=403)
    if timezone not in ZONES:
        return JSONResponse({"ok": False, "detail": "That timezone is not on the list."})
    if preferred_language not in language.LANGUAGES:
        return JSONResponse({"ok": False, "detail": "That language is not on the list."})
    url, token = plex_url.strip().rstrip("/"), plex_token.strip()
    if not url or not token:
        return JSONResponse({"ok": False,
                             "detail": "Both the address and the token are needed."})
    # Test the candidates without saving them. Writing first and reverting on
    # failure left a window for the sync loop to run against them.
    ok, detail = _test_plex(url, token)
    if not ok:
        return JSONResponse({"ok": False, "detail": detail})
    await _save_and_sync(url, token, timezone, preferred_language)
    return JSONResponse({"ok": True, "detail": detail})


# ---------- sign in ----------

def _set_session(resp, token, secure=False):
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure,
                    max_age=auth.SESSION_TTL, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, error: str = ""):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=True, error=error, nav="")


@router.post("/setup")
def setup_save(request: Request, username: str = Form(""), password: str = Form("")):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    try:
        uid = auth.create_user(username, password, role="admin")
    except ValueError as e:
        return RedirectResponse(f"/setup?error={urllib.parse.quote(str(e))}",
                                status_code=303)
    return _set_session(RedirectResponse("/", status_code=303), auth.create_session(uid),
                        secure=exposure.request_is_https(request))


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if auth.mode() == "none":
        return RedirectResponse("/", status_code=303)
    if auth.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=False, error=error, nav="")


@router.post("/login")
def login_save(request: Request, username: str = Form(""), password: str = Form("")):
    addr = exposure.client_address(request)
    keys = [ratelimit.addr_key(addr)]
    # An account's own lock is for people outside. Someone at home who mistypes
    # must not be able to lock the owner out of it from the internet.
    if not exposure.is_home(request):
        keys.append("user:" + (username or "").strip().lower())
    wait = ratelimit.reserve(keys)
    if wait:
        resp = page(request, "signin.html", setup=False, error=ratelimit.say(wait),
                    nav="")
        resp.status_code = 429
        resp.headers["Retry-After"] = str(int(wait) + 1)
        return resp
    user = auth.verify(username, password)
    if not user:
        who = (username or "").strip()[:60] if auth.user_exists(username) else "unknown account"
        log.warning("sign-in failed for %s from %s", who, addr)
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote("that username and password do not match"),
            status_code=303)
    # The address keeps what it was given, less this try; the account starts over.
    ratelimit.refund(keys[0])
    for k in keys[1:]:
        ratelimit.ok(k)
    # Secure over HTTPS only: a plain-HTTP sign-in on the LAN must keep its cookie.
    return _set_session(RedirectResponse("/", status_code=303),
                        auth.create_session(user["id"]),
                        secure=exposure.request_is_https(request))


@router.post("/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@router.post("/api/theme")
def api_theme(request: Request, theme: str = Form("dark")):
    """Remember the theme against the account, when there is one.

    With sign-in off the browser keeps it in local storage instead, which is
    the only place it can live when nobody is identified.
    """
    theme = theme if theme in ("light", "dark") else "dark"
    user = current_user(request)
    if user:
        auth.set_pref(user["id"], "theme", theme)
        return JSONResponse({"ok": True, "stored": "account"})
    return JSONResponse({"ok": True, "stored": "browser"})


