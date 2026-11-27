"""Settings: the Plex connection, accounts, channel artwork, sync."""
import asyncio
import concurrent.futures as cf
import os
import tempfile
import threading
import time
import urllib.parse

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import (
    acme_tls,
    announce,
    auth,
    cf_access,
    db,
    exposure,
    folders,
    handover,
    health,
    language,
    notify,
    passes,
    plextv,
    recorder,
    sync,
    tuner,
)
from ..plex import Plex, PlexError
from ._shared import ZONES, current_user, page

router = APIRouter()

# ---------- settings ----------

def _channel_rows():
    """Every channel, and where its logo comes from."""
    out = []
    for c in db.query("SELECT * FROM channels ORDER BY CAST(vcn AS REAL), vcn"):
        custom = bool(c["custom_logo"] and os.path.exists(c["custom_logo"]))
        theirs = bool(c["logo_path"] and os.path.exists(c["logo_path"]))
        out.append({
            "vcn": c["vcn"], "call_sign": c["call_sign"] or "",
            "network": c["network"] or "",
            "language": language.effective(c["language_override"], c["language"],
                                           passes.preferred_language()),
            "language_detected": c["language"] or "",
            "language_override": c["language_override"] or "",
            "custom": custom, "has_logo": custom or theirs,
            "source": "yours" if custom else ("guide" if theirs else "none"),
            "v": c["custom_logo_at"] or c["logo_fetched_at"] or 0,
        })
    return out


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tested: str = "", autherror: str = "",
                  notified: str = ""):
    return page(request, "settings.html", zones=ZONES, languages=language.LANGUAGES,
                tested=tested,
                autherror=autherror, notified=notified,
                logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                dests=notify.destinations(), catalog=notify.CATALOG,
                faults=notify.FAULTS, activity=notify.ACTIVITY,
                cf_ready=cf_access.available())


@router.get("/partial/settings", response_class=HTMLResponse)
def settings_partial(request: Request, tested: str = "", autherror: str = "",
                     notified: str = ""):
    """The settings window on its own, for the overlay the gear opens."""
    return page(request, "_settings.html", zones=ZONES, languages=language.LANGUAGES,
                tested=tested,
                autherror=autherror, notified=notified,
                logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                dests=notify.destinations(), catalog=notify.CATALOG,
                faults=notify.FAULTS, activity=notify.ACTIVITY,
                cf_ready=cf_access.available())


# What each setting may hold. The web forms and the television apps both save
# through `apply`, so neither can store a value the other would refuse.
CHOICES = {
    "recorder_default": ("auto", "plex", "couchelephant"),
    "recorder_encoder": ("auto", "software", "vaapi"),
}
# Whole minutes or days: (lowest, highest).
NUMBERS = {
    "sync_minutes": (5, 1440),
    "recorder_lead_minutes": (0, 240),
    "recorder_trail_minutes": (0, 240),
    "recorder_keep_days": (0, 240),
    "live_pause_minutes": (5, 240),
    "overtime_cap_minutes": (0, 360),
}
# What `apply` takes. Sign-in, accounts and secrets are not here on purpose,
# except the Plex token, which only the web form sends.
KEYS = ("plex_url", "plex_token", "timezone", "preferred_language", "dry_run", "tuner_url",
        "recorder_shows_folder", "recorder_movies_folder",
        "recorder_shows_section", "recorder_movies_section",
        "sportsdb_key", "adskip_live", "adskip_final", "sports_overtime", "recorder_paused", "announce", *CHOICES, *NUMBERS)


def apply(values: dict) -> list[str]:
    """Save the settings given, and only those. Returns what was refused."""
    refused = []
    for key, value in values.items():
        if key not in KEYS or value is None:
            continue
        value = str(value).strip()
        if key in NUMBERS:
            low, high = NUMBERS[key]
            try:
                value = str(max(low, min(high, int(value))))
            except ValueError:
                refused.append(key)
                continue
        elif key in CHOICES:
            if value not in CHOICES[key]:
                refused.append(key)
                continue
        elif key == "timezone":
            if value not in ZONES:
                refused.append(key)
                continue
        elif key == "preferred_language":
            if value not in language.LANGUAGES:
                refused.append(key)
                continue
        elif key in ("dry_run", "adskip_live", "adskip_final", "sports_overtime",
                     "recorder_paused", "announce"):
            value = "1" if value in ("1", "true", "on") else "0"
        elif key in ("plex_url", "tuner_url"):
            value = value.rstrip("/")
        elif key == "plex_token":
            # A masked token is a form echoing back what it was shown.
            if not value or value.startswith("*"):
                continue
        elif key in ("recorder_shows_folder", "recorder_movies_folder"):
            # A folder inside the library mount, and nothing that climbs out of it.
            # Without a mount the folder is a full path, so it keeps its start.
            whole = value.startswith("/") and not handover.LIB_DIR
            value = "/".join(p for p in value.strip("/").split("/")
                             if p and p not in (".", ".."))
            if whole:
                if not value:
                    refused.append(key)     # "/" alone is the whole disk
                    continue
                value = "/" + value
        before = db.get_setting(key)
        db.set_setting(key, value)
        if key == "announce" and before != value:
            announce.refresh(retry=True)
    return refused


@router.post("/settings")
def settings_save(plex_url: str = Form(""), plex_token: str = Form(""),
                  timezone: str = Form("UTC"), sync_minutes: str = Form("60"),
                  dry_run: str = Form("0"), preferred_language: str = Form("")):
    # An empty language is "not sent": the Plex tab's form does not carry it.
    apply({"plex_url": plex_url, "plex_token": plex_token, "timezone": timezone,
           "sync_minutes": sync_minutes, "dry_run": dry_run,
           "preferred_language": preferred_language or None})
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/announce")
def settings_announce(announce_on: str = Form("0")):
    apply({"announce": announce_on})
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/recorder")
def settings_recorder(tuner_url: str = Form(""), recorder_default: str = Form("auto"),
                      recorder_encoder: str = Form("auto"),
                      recorder_lead_minutes: str = Form("1"),
                      recorder_trail_minutes: str = Form("3"),
                      recorder_shows_folder: str = Form(""),
                      recorder_shows_section: str = Form(""),
                      recorder_movies_folder: str = Form(""),
                      recorder_movies_section: str = Form(""),
                      recorder_keep_days: str = Form("2"),
                      live_pause_minutes: str = Form("30"),
                      adskip_live: str = Form("0"), adskip_final: str = Form("0"),
                      sports_overtime: str = Form("0"),
                      overtime_cap_minutes: str = Form("180")):
    """CouchElephant's own recorder: the tuner, the default, the handover."""
    apply(dict(tuner_url=tuner_url, recorder_default=recorder_default,
               recorder_encoder=recorder_encoder,
               recorder_lead_minutes=recorder_lead_minutes,
               recorder_trail_minutes=recorder_trail_minutes,
               recorder_shows_folder=recorder_shows_folder,
               recorder_shows_section=recorder_shows_section,
               recorder_movies_folder=recorder_movies_folder,
               recorder_movies_section=recorder_movies_section,
               recorder_keep_days=recorder_keep_days,
               live_pause_minutes=live_pause_minutes,
               adskip_live=adskip_live, adskip_final=adskip_final,
               sports_overtime=sports_overtime,
               overtime_cap_minutes=overtime_cap_minutes))
    # Setup: look at the chosen library's folders now, on a thread of its own.
    folders.rescan_soon(True)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/recorder/pause")
def settings_recorder_pause(on: str = Form("1")):
    """Stop our own recorder starting anything new (the tray's switch). A
    recording already running finishes, and Plex is not touched."""
    apply({"recorder_paused": on})
    return {"ok": True, "paused": db.get_setting("recorder_paused") == "1"}


@router.get("/api/recorder/status")
def recorder_status():
    """Everything the Recorder settings show: whether it can run and why not,
    what the tuner says, and the choices for the handover."""
    out = {"ok": True, "available": recorder.available(),
           "why": recorder.why_unavailable(),
           "paused": db.get_setting("recorder_paused") == "1",
           "tuner": {"url": tuner.base() or "",
                     "typed": bool((db.get_setting("tuner_url") or "").strip())},
           "library": {"mounted": bool(handover.LIB_DIR) and os.path.isdir(handover.LIB_DIR),
                       "path": handover.LIB_DIR, "folders": [], "sections": []},
           "handover_why": handover.why_unavailable() if recorder.available() else None}
    if not out["tuner"]["url"] and db.get_setting("plex_url") and db.get_setting("plex_token"):
        try:
            with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
                out["tuner"]["url"] = tuner.detect(plex) or ""
        except Exception as e:
            out["tuner"]["error"] = f"Could not ask Plex for its tuner: {e}"
    if out["tuner"]["url"]:
        try:
            rows = tuner.tuners()
            out["tuner"]["total"] = len(rows)
            out["tuner"]["free"] = tuner.free()
        except tuner.TunerError as e:
            out["tuner"]["error"] = str(e)
    if out["available"]:
        out["encoder"] = recorder.encoder()
    if out["library"]["mounted"]:
        try:
            out["library"]["folders"] = sorted(
                n for n in os.listdir(handover.LIB_DIR)
                if os.path.isdir(os.path.join(handover.LIB_DIR, n))
                and not n.startswith("."))
        except OSError as e:
            out["library"]["error"] = f"Cannot read {handover.LIB_DIR}: {e}"
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            out["library"]["sections"] = [
                {"key": str(d.get("key")), "title": d.get("title") or "",
                 "type": d.get("type") or "",
                 "paths": [p.get("path") for p in d.get("Location") or []]}
                for d in plex.library_sections() if d.get("type") in ("show", "movie")]
    except Exception as e:
        out["library"]["plex_error"] = f"Could not ask Plex for its libraries: {e}"
    return JSONResponse(out)


def _writable(path: str) -> str:
    """Empty when a file can be made in the folder, else the reason it cannot."""
    if not os.path.isdir(path):
        return "That folder does not exist on this computer."
    try:
        fd, name = tempfile.mkstemp(dir=path, prefix=".couchelephant-check-")
    except OSError:
        return "CouchElephant cannot write to that folder."
    os.close(fd)
    try:
        os.remove(name)
    except OSError:
        return "CouchElephant can write there but cannot remove its test file."
    return ""


def _inside_mount(plex_path: str) -> str | None:
    """Where Plex's folder is under the library mount, as a name inside it.

    Plex and this container may see the disk at different places. Try the
    path's tail under the mount: /data/media/TV becomes TV, then media/TV,
    and the first one that is a folder wins.
    """
    parts = [p for p in plex_path.split("/") if p]
    for i in range(len(parts) - 1, -1, -1):
        rel = "/".join(parts[i:])
        if os.path.isdir(os.path.join(handover.LIB_DIR, rel)):
            return rel
    return None


@router.get("/api/recorder/library-suggest")
def recorder_library_suggest():
    """The library folders Plex reports, and whether each can be written here."""
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            sections = plex.library_sections()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"Could not ask Plex for its libraries: {e}"})
    out = []
    for d in sections:
        kind = {"show": "shows", "movie": "movies"}.get(d.get("type"))
        places = [p.get("path") for p in d.get("Location") or [] if p.get("path")]
        if not kind or not places:
            continue
        path, why = places[0], ""
        if handover.LIB_DIR:
            # The first place that maps into the mount, else the first one Plex lists.
            for p in places:
                rel = _inside_mount(p)
                if rel is not None:
                    path = rel
                    break
            else:
                why = "That folder does not exist on this computer."
            if not why:
                why = _writable(os.path.join(handover.LIB_DIR, path))
        else:
            for p in places:
                if os.path.isdir(p):
                    path = p
                    break
            why = _writable(path)
        out.append({"kind": kind, "section_id": str(d.get("key")),
                    "section": d.get("title") or "", "path": path,
                    "writable": not why, "why": why})
    return JSONResponse({"ok": True, "suggestions": out,
                         "user_advice": folders.user_advice()})


@router.post("/settings/sources")
def settings_sources(sportsdb_key: str = Form(""), tmdb_key: str = Form("")):
    """The two optional keys, on a route of their own.

    Not fields on `settings_save`: that one writes every Plex setting it is
    handed, so a form posting to it without carrying the server URL, the
    timezone and the rest as hidden fields would blank them.
    """
    apply({"sportsdb_key": sportsdb_key})
    # Masked on the way out, so a form echoing asterisks back must not be saved
    # over the real key. Same rule as `plex_token`.
    if tmdb_key.strip() and not tmdb_key.startswith("*"):
        db.set_setting("tmdb_key", tmdb_key.strip())
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/auth")
def settings_auth(request: Request, auth_mode: str = Form("none"),
                  cf_team_domain: str = Form(""), cf_aud: str = Form("")):
    """Change how, or whether, people sign in.

    Turning sign-in on with no accounts sends the next visitor to create one.
    Turning it off is deliberately allowed from inside: someone locked out of
    Cloudflare Access can still reach the box on the LAN and switch back.
    """
    mode = auth_mode if auth_mode in auth.MODES else "none"
    # While the way in is open, sign-in cannot be weakened under it.
    if exposure.away_on() and (mode == "none" or (
            mode == "local" and _away_blocker("local"))):
        return RedirectResponse(
            "/settings?autherror=" + urllib.parse.quote(
                "Turn off Use away from home first."), status_code=303)
    if mode == "cloudflare":
        ok, detail = cf_access.check(cf_team_domain.strip(), cf_aud.strip())
        if not ok:
            return RedirectResponse(
                "/settings?autherror=" + urllib.parse.quote(detail), status_code=303)
    db.set_setting("cf_team_domain", cf_team_domain.strip())
    db.set_setting("cf_aud", cf_aud.strip())
    db.set_setting("auth_mode", mode)
    return RedirectResponse("/settings", status_code=303)


def _away_blocker(mode=None):
    """Why the switch may not turn on yet, or None. `mode` is the sign-in
    mode to judge, the current one by default."""
    mode = mode or auth.mode()
    if mode == "cloudflare":
        return None
    if mode == "none" or auth.user_count() == 0:
        return {"ok": False, "fix": "make_account",
                "error": "Make an account first. Away from home needs a sign-in."}
    bare = [{"id": u["id"], "username": u["username"]}
            for u in auth.list_users() if not u["has_password"]]
    if bare:
        names = ", ".join(u["username"] for u in bare)
        return {"ok": False, "fix": "set_passwords", "users": bare,
                "error": f"Give a password to {names} first."}
    return None


@router.post("/settings/away")
def settings_away(on: str = Form("0")):
    """The Use away from home switch. It will not turn on while a way in
    from outside has no password."""
    if on != "1":
        db.set_setting("away_mode", "0")
        announce.refresh(retry=True)
        return {"ok": True}
    blocked = _away_blocker()
    if blocked:
        return blocked
    db.set_setting("away_mode", "1")
    announce.refresh(retry=True)
    return {"ok": True}


@router.post("/settings/away/domain")
def settings_away_domain(domain: str = Form("")):
    """The name to get a real certificate for. Saving starts the order in the
    background; the page asks /api/away/domain how it is going. Empty drops
    back to the own certificate. The retry button posts the same name."""
    if not domain.strip():
        db.set_setting("away_domain", "")
        acme_tls.drop_error()
        return {"ok": True}
    try:
        name = acme_tls.clean_domain(domain)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    if not exposure.away_on():
        return JSONResponse({"ok": False,
                             "error": "Turn on Use away from home first."})
    wait = acme_tls.too_soon(name)
    if wait:
        return JSONResponse({"ok": False, "error":
                             f"Let's Encrypt was asked a little while ago. Try again in "
                             f"{max(1, wait // 60)} minutes."})
    db.set_setting("away_domain", name)
    acme_tls.drop_error()
    acme_tls.start_in_background(name)
    return {"ok": True, "domain": name}


@router.post("/settings/away/proxies")
def settings_away_proxies(proxies: str = Form("")):
    """Trusted proxies: the addresses of a reverse proxy or tunnel that sits in
    front of this server. Only these may say who the real visitor is."""
    nets, error = exposure.parse_trusted(proxies)
    if error:
        return JSONResponse({"ok": False, "error": error})
    db.set_setting("trusted_proxies", ", ".join(p.strip() for p in proxies.split(",")
                                                if p.strip()))
    exposure.clear_forward()
    return {"ok": True, "count": len(nets)}


@router.get("/api/away/domain")
def away_domain_state():
    return acme_tls.status()


@router.post("/settings/away/account")
def settings_away_account(request: Request, username: str = Form(""), password: str = Form("")):
    """The fix for a switch with no sign-in: Local accounts on, and the first
    account made, with this browser signed in to it."""
    if auth.mode() == "cloudflare" or (auth.mode() == "local" and auth.user_count()):
        return JSONResponse({"ok": False, "error": "There is already a way to sign in."})
    try:
        uid = auth.create_user(username, password, role="admin")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db.set_setting("auth_mode", "local")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.SESSION_COOKIE, auth.create_session(uid), httponly=True,
                    samesite="lax", max_age=auth.SESSION_TTL, path="/",
                    secure=exposure.request_is_https(request))
    return resp


@router.post("/settings/away/password")
def settings_away_password(uid: int = Form(...), password: str = Form("")):
    """The fix for an account with no password. Only such an account: a
    password that exists is changed by its owner, not from here."""
    row = next((u for u in auth.list_users() if u["id"] == uid), None)
    if not row or row["has_password"]:
        return JSONResponse({"ok": False, "error": "That account is not waiting for a password."})
    try:
        auth.set_password(uid, password)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


def _encryption_item(request):
    st = acme_tls.status()
    https = request.url.scheme == "https"
    if st["state"] == "ok":
        return {"name": "Encryption", "ok": True,
                "say": f"The real certificate for {st['domain']} is valid until "
                       f"{st['until']}. It renews itself."}
    if st["state"] == "error":
        return {"name": "Encryption", "ok": False,
                "say": f"The certificate for {st['domain']} is not ready: "
                       f"{st['say']} Until then browsers will warn; the apps "
                       "still check this server's own certificate."}
    if https:
        return {"name": "Encryption", "ok": True,
                "say": "This connection is encrypted with this server's own "
                       "certificate. The apps check it, but browsers will warn. "
                       "Add a domain for a real one."}
    return {"name": "Encryption", "ok": False,
            "say": "This connection is not encrypted. Passwords cross the "
                   "internet in the clear until HTTPS is on."}


def _proxy_items():
    """A proxy nobody listed: its visitors are treated as outside."""
    peer = exposure.untrusted_forward()
    if not peer:
        return []
    return [{"name": "Proxy", "ok": False,
             "say": f"A request came through a proxy at {peer} that is not listed. "
                    "It was treated as coming from outside. If you trust that proxy, "
                    "add its address under Trusted proxies, just above."}]


@router.get("/api/away/check")
def away_check(request: Request):
    """What is safe and what is not, in plain words, for the self-check."""
    users = auth.list_users()
    mode = auth.mode()
    signin = mode == "cloudflare" or (mode == "local" and users
                                     and all(u["has_password"] for u in users))
    if mode == "cloudflare" and not (db.get_setting("trusted_proxies") or "").strip():
        settings_item = {"name": "Settings", "ok": False,
                         "say": "A Cloudflare tunnel on this network makes every visitor "
                                "look home. Add the tunnel's address under Trusted "
                                "proxies, just above."}
    else:
        settings_item = {"name": "Settings", "ok": True,
                         "say": "Settings and setup only open from your home network. "
                                "A paired TV can still change its own settings."}
    items = [
        {"name": "Sign-in", "ok": bool(signin),
         "say": "Everyone who comes from outside must sign in." if signin
         else "Someone from outside could get in without a password."},
        {"name": "Guessing", "ok": True,
         "say": "Five wrong passwords in ten minutes lock sign-in for a minute, "
                "then longer each time."},
        settings_item,
        {"name": "Paired devices", "ok": True,
         "say": "Paired TVs and phones keep working with their own keys."},
        _encryption_item(request),
        *_proxy_items(),
        {"name": "Router", "ok": False,
         "say": "Your router may send home devices through the internet address; "
                "they will be asked to sign in."},
    ]
    return {"items": items}


@router.post("/settings/users/{uid}/delete")
def settings_user_delete(request: Request, uid: int):
    me = current_user(request)
    if me and me["id"] == uid:
        return RedirectResponse(
            "/settings?autherror=" + urllib.parse.quote("you cannot delete your own account"),
            status_code=303)
    if exposure.away_on() and auth.mode() == "local" and auth.user_count() <= 1:
        return RedirectResponse(
            "/settings?autherror=" + urllib.parse.quote(
                "Turn off Use away from home first."), status_code=303)
    auth.delete_user(uid)
    return RedirectResponse("/settings", status_code=303)


def _test_plex(url=None, token=None, timeout=None):
    """Check a server, and say something useful when it does not answer.

    Each step is separate so the reason names the step that failed. A single
    try around the lot can only ever report the last exception, which is how a
    bad token comes to read as "server unreachable".

    Candidate values can be passed in. First run does that rather than saving
    them, testing, and reverting, which left a window for the sync loop to run
    against half-entered credentials.
    """
    url = url if url is not None else db.get_setting("plex_url")
    token = token if token is not None else db.get_setting("plex_token")
    if not url:
        return False, "No server address set. Fill in the address above and save."
    if not token:
        return False, "No token set. Paste the token above and save."

    with Plex(url, token, timeout=timeout) as plex:
        return _probe(plex, url)


def _probe(plex, url):
    try:
        info = plex.server_info()
    except PlexError as e:
        if getattr(e, "status", None) in (401, 403):
            return False, ("The server answered but rejected the token. Check the "
                           "PlexOnlineToken in Preferences.xml on the server.")
        return False, f"Could not reach {url}. {e}"
    except Exception as e:
        # Almost always a wrong host, a wrong port, or nothing listening.
        return False, (f"Could not reach {url}. {type(e).__name__}: {e}. "
                       "The address has to work from inside this container, so "
                       "127.0.0.1 only works if Plex runs in it too.")

    name = info.get("friendlyName") or "the server"
    version = info.get("version") or "unknown version"
    try:
        dvrs = plex.dvrs()
    except Exception as e:
        return False, (f"Reached {name} (Plex {version}), but could not read its "
                       f"DVRs: {type(e).__name__}: {e}")
    if not dvrs:
        return False, (f"Reached {name} (Plex {version}), but it has no DVR. "
                       "Add a tuner to Plex before CouchElephant can record.")

    lineup = dvrs[0].get("lineupTitle") or "a lineup"
    return True, (f"{name}, Plex {version}. "
                  f"{len(dvrs)} DVR{'' if len(dvrs) == 1 else 's'}, {lineup}.")


# ---------- sign in with Plex ----------

HELD_SECONDS = 20 * 60
_held = {}                      # pin id -> {"at", "expires", "token"}
_held_lock = threading.Lock()


def _prune(now):
    for k in [k for k, v in _held.items() if now - v["at"] > HELD_SECONDS]:
        del _held[k]


def _drop(pin):
    with _held_lock:
        _held.pop(pin, None)


def _mark_used(pin):
    """Keep the pin on record, without its token, so a late poll cannot
    take the token again."""
    with _held_lock:
        _held[pin] = {"at": time.time(), "expires": 0, "token": None, "used": True}


def _held_token(pin):
    with _held_lock:
        _prune(time.time())
        e = _held.get(pin)
        return e["token"] if e else None


async def _save_and_sync(url, token, timezone=None, preferred_language=None):
    """Save a tested server and start the first guide pull. First run and
    Sign in with Plex both end here."""
    first_run = not db.get_setting("plex_url")
    db.set_setting("plex_url", url)
    db.set_setting("plex_token", token)
    if timezone is not None:
        db.set_setting("timezone", timezone)
    if preferred_language in language.LANGUAGES:
        db.set_setting("preferred_language", preferred_language)
    # Preview mode starts on for a new install. A configured one keeps its choice.
    if first_run:
        db.set_setting("dry_run", "1")
    # Pull the guide now rather than waiting for the loop's next turn. Without
    # this a new install lands on an empty grid and has no idea whether it is
    # broken or just early.
    asyncio.create_task(asyncio.to_thread(sync.full_sync))


@router.post("/settings/plex/signin")
def plex_signin_start():
    try:
        pin = plextv.create_pin()
    except plextv.PlexTvError as e:
        return JSONResponse({"ok": False, "detail": str(e)})
    now = time.time()
    with _held_lock:
        _prune(now)
        _held[pin.id] = {"at": now, "expires": pin.expires_at, "token": None}
    return {"ok": True, "pin": pin.id, "code": pin.code, "link": "https://plex.tv/link"}


@router.get("/settings/plex/signin/{pin}")
def plex_signin_poll(pin: int):
    now = time.time()
    token = _held_token(pin)
    if token is None:
        with _held_lock:
            e = _held.get(pin)
            if e is None:
                # Not a pin this install started. Never ask plex.tv about it.
                return {"ok": False, "state": "expired"}
            if e.get("used"):
                return {"ok": False, "state": "expired"}
            if e["expires"] <= now:
                del _held[pin]
                return {"ok": False, "state": "expired"}
        try:
            token = plextv.check_pin(pin)
        except plextv.PlexTvError as e:
            if e.status == 404:
                _drop(pin)
                return {"ok": False, "state": "expired"}
            return {"ok": False, "state": "error", "detail": str(e)}
        if token is None:
            return {"ok": True, "state": "waiting"}
        with _held_lock:
            _held[pin] = {"at": now, "expires": 0, "token": token}
    try:
        found = plextv.servers(token)
    except plextv.PlexTvError as e:
        return {"ok": False, "state": "error", "detail": str(e)}
    mine = [s for s in found if s.owned]
    out = {"ok": True, "state": "servers",
           "servers": [{"name": s.name, "id": s.client_id} for s in mine]}
    if not mine:
        out["detail"] = NOT_OWNED
    return out


NOT_OWNED = ("This Plex account does not own a server. Sign in with the account "
             "that runs your Plex server.")
RELAY_ONLY = ("Only Plex's relay answered. CouchElephant needs a direct address. "
              "Check that this computer can reach the Plex server, or enter it "
              "yourself.")

ADDRESS_TIMEOUT = 4.0       # per request to one address
USE_CAP = 10.0              # all addresses together


def _safe_test(url, token):
    try:
        return _test_plex(url, token, ADDRESS_TIMEOUT)
    except Exception as e:
        return False, f"Could not reach {url}. {type(e).__name__}"


def _try_all(urls, token):
    """Test every address at once and answer in the order given. The first
    that passes in that order wins, not the fastest, so a local address still
    beats a relay. One that has not answered by the cap counts as failed."""
    miss = "Could not reach {}. It did not answer in time."
    pool = cf.ThreadPoolExecutor(max_workers=max(1, len(urls)))
    futs = [pool.submit(_safe_test, u, token) for u in urls]
    deadline = time.monotonic() + USE_CAP

    def settled():
        # Every address before the first pass has answered, or all have.
        for f in futs:
            if not f.done():
                return False
            if f.result()[0]:
                return True
        return True

    try:
        while not settled() and time.monotonic() < deadline:
            cf.wait([f for f in futs if not f.done()],
                    timeout=max(0.0, deadline - time.monotonic()),
                    return_when=cf.FIRST_COMPLETED)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [f.result() if f.done() else (False, miss.format(u))
            for u, f in zip(urls, futs)]


@router.post("/settings/plex/signin/{pin}/use")
async def plex_signin_use(pin: int, server: str = Form(""), timezone: str = Form(""),
                          preferred_language: str = Form("")):
    token = _held_token(pin)
    if token is None:
        return JSONResponse({"ok": False, "tried": [],
                             "detail": "That sign-in has run out. Sign in again."})
    try:
        found = await asyncio.to_thread(plextv.servers, token)
    except plextv.PlexTvError as e:
        return JSONResponse({"ok": False, "tried": [], "detail": str(e)})
    chosen = next((s for s in found if s.owned and s.client_id == server), None)
    if chosen is None:
        return JSONResponse({"ok": False, "tried": [],
                             "detail": "Plex no longer lists that server."})
    urls, relay = _addresses(chosen.connections)
    results = await asyncio.to_thread(_try_all, urls, chosen.access_token)
    answered_relay = False
    for url, (ok, detail) in zip(urls, results):
        if ok and url in relay:
            answered_relay = True      # reachable, but never saved
        elif ok:
            await _save_and_sync(url, chosen.access_token,
                                 timezone if timezone in ZONES else None,
                                 preferred_language)
            _mark_used(pin)
            return JSONResponse({"ok": True, "detail": detail, "address": url})
    if answered_relay:
        return JSONResponse({"ok": False, "detail": RELAY_ONLY, "tried": urls})
    failures = [d for _, d in results]
    # Prefer a reason from a server that answered over "could not reach".
    why = next((d for d in failures if not d.startswith("Could not reach")),
               failures[0] if failures else "Plex lists no address for this server.")
    return JSONResponse({"ok": False, "detail": why, "tried": urls})


def _addresses(conns):
    """Every address to try, best first, and the set that are relays. A local
    plex.direct name is followed by the plain http address, because many home
    routers will not resolve plex.direct (DNS rebinding protection)."""
    urls, relay = [], set()
    for c in conns:
        u = c.uri.rstrip("/")
        if u and u not in urls:
            urls.append(u)
            if c.relay:
                relay.add(u)
        if c.local and not c.relay and c.address and c.port:
            host = f"[{c.address}]" if ":" in c.address else c.address
            plain = f"http://{host}:{c.port}"
            if plain not in urls:
                urls.append(plain)
    return urls, relay


@router.post("/settings/test")
def settings_test(request: Request):
    ok, detail = _test_plex()
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": ok, "detail": detail})
    prefix = "OK: " if ok else "FAILED: "
    return RedirectResponse("/settings?tested=" + urllib.parse.quote(prefix + detail),
                            status_code=303)


# What a browser will actually render, by the file's own first bytes rather
# than by the name it arrived under.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_MAX_LOGO = 2 * 1024 * 1024


def _sniff_image(blob):
    for magic, kind in _IMAGE_MAGIC:
        if blob.startswith(magic):
            return kind
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _is_svg(blob):
    """SVG is refused, not accepted and then broken.

    It cannot be served as image/png, and serving an uploaded document as
    image/svg+xml runs any script inside it on this origin.
    """
    head = blob.lstrip()[:200].lower()
    return head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in head


@router.post("/settings/channels/{vcn}/logo")
async def channel_logo_upload(vcn: str, logo: UploadFile = File(...)):
    """Use a logo of your own for this channel."""
    row = db.one("SELECT vcn, custom_logo FROM channels WHERE vcn = ?", (vcn,))
    if not row:
        return JSONResponse({"ok": False, "error": "unknown channel"}, status_code=404)
    blob = await logo.read()
    if not blob:
        return JSONResponse({"ok": False, "error": "that file is empty"}, status_code=400)
    if len(blob) > _MAX_LOGO:
        return JSONResponse({"ok": False, "error":
                             f"that file is {len(blob)//1024} KB. The limit is "
                             f"{_MAX_LOGO//1024} KB."}, status_code=400)
    # Trust the bytes, not the extension. A file named .png that is not one
    # would render as a broken image on every page that shows the channel.
    kind = _sniff_image(blob)
    if not kind:
        if _is_svg(blob):
            return JSONResponse({"ok": False, "error":
                                 "SVG cannot be used here. Save it as a PNG and "
                                 "upload that."}, status_code=400)
        return JSONResponse({"ok": False, "error":
                             "that is not an image file (PNG, JPEG, GIF or WebP)."},
                            status_code=400)

    os.makedirs(sync.LOGO_DIR, exist_ok=True)
    safe = "".join(c if (c.isalnum() and c.isascii()) or c in "._-" else "_" for c in vcn)
    path = os.path.join(sync.LOGO_DIR, f"custom-{safe}.{kind}")
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    # A different extension than last time leaves the old file behind.
    old = row["custom_logo"]
    if old and old != path and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    with db.tx() as c:
        c.execute("UPDATE channels SET custom_logo = ?, custom_logo_at = ? WHERE vcn = ?",
                  (path, int(time.time()), vcn))
    return JSONResponse({"ok": True, "message": f"Logo set for {vcn}.",
                         "v": int(time.time())})


@router.post("/settings/channels/{vcn}/logo/reset")
def channel_logo_reset(vcn: str):
    """Go back to whatever the guide provides for this channel."""
    row = db.one("SELECT custom_logo FROM channels WHERE vcn = ?", (vcn,))
    if not row:
        return JSONResponse({"ok": False, "error": "unknown channel"}, status_code=404)
    if row["custom_logo"] and os.path.exists(row["custom_logo"]):
        try:
            os.remove(row["custom_logo"])
        except OSError:
            pass
    with db.tx() as c:
        c.execute("UPDATE channels SET custom_logo = NULL, custom_logo_at = NULL "
                  "WHERE vcn = ?", (vcn,))
    has = db.one("SELECT logo_path FROM channels WHERE vcn = ?", (vcn,))
    back = bool(has and has["logo_path"] and os.path.exists(has["logo_path"]))
    return JSONResponse({"ok": True, "v": int(time.time()),
                         "message": (f"{vcn} is back to the guide's logo." if back
                                     else f"{vcn} has no logo again. The guide offers none.")})


def set_channel_language(vcn: str, code: str) -> tuple[bool, str]:
    """Save a channel's language by hand. An empty code goes back to what was
    detected. Returns (ok, error)."""
    code = (code or "").strip().lower()
    if code and code not in language.LANGUAGES:
        return False, "That language is not on the list."
    if not db.one("SELECT 1 FROM channels WHERE vcn = ?", (vcn,)):
        return False, "No such channel."
    with db.tx() as c:
        c.execute("UPDATE channels SET language_override = ? WHERE vcn = ?",
                  (code or None, vcn))
    return True, ""


def channel_languages() -> list[dict]:
    """Every channel with the language it counts as, what was detected, and
    the person's own choice (empty when there is none)."""
    return [{"vcn": c["vcn"], "call_sign": c["call_sign"], "network": c["network"],
             "language": c["language"], "language_detected": c["language_detected"],
             "language_override": c["language_override"]} for c in _channel_rows()]


@router.get("/api/channels/languages")
def api_channel_languages():
    return {"ok": True, "preferred_language": passes.preferred_language(),
            "languages": [{"code": k, "name": v[0], "english": v[1]}
                          for k, v in language.LANGUAGES.items()],
            "channels": channel_languages()}


@router.post("/settings/channels/{vcn}/language")
def channel_language_set(vcn: str, language_override: str = Form("")):
    ok, why = set_channel_language(vcn, language_override)
    if not ok:
        return JSONResponse({"ok": False, "error": why}, status_code=400)
    row = next(c for c in _channel_rows() if c["vcn"] == vcn)
    return {"ok": True, "language": row["language"],
            "language_detected": row["language_detected"],
            "language_override": row["language_override"]}


@router.post("/settings/logos")
async def refetch_logos():
    """Force every channel logo to be downloaded again."""
    await asyncio.to_thread(sync.cache_logos, True)
    return RedirectResponse("/settings", status_code=303)


@router.get("/api/notices")
def api_notices():
    """What is wrong with Plex right now, and how long it has been wrong.

    Read by the badge on the sync button. A notice lives until the check that
    raised it passes again, so this list is the current truth rather than a
    log of things that once happened.
    """
    now = int(time.time())
    out = []
    for n in health.open_notices():
        out.append({
            "code": n["code"],
            "severity": n["severity"],
            "title": n["title"],
            "detail": n["detail"],
            "hint": n["hint"],
            "since": n["first_seen"],
            "age_seconds": max(0, now - (n["first_seen"] or now)),
        })
    return JSONResponse({"notices": out,
                         "bad": sum(1 for n in out if n["severity"] == "bad")})


@router.post("/sync")
async def sync_now():
    await asyncio.to_thread(sync.full_sync)
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/", status_code=303)




# ---------- notifications ----------

@router.post("/settings/notify/save")
def notify_save(dest_id: str = Form(""), name: str = Form(""),
                kind: str = Form("discord"), webhook: str = Form(""),
                token: str = Form(""), chat_id: str = Form(""),
                remind_hours: str = Form("24"),
                events: list[str] = Form([])):
    """Create or update one destination.

    A masked secret is a form echoing back what it was shown, not a new value,
    so it is dropped rather than saved. Same rule as `plex_token`.
    """
    def real(v):
        v = (v or "").strip()
        return "" if v.startswith("*") else v
    try:
        notify.save_destination(
            dest_id=int(dest_id) if dest_id.strip() else None,
            name=name, kind=kind, events=events,
            remind_hours=int(remind_hours or 0),
            webhook=real(webhook), token=real(token),
            chat_id=(chat_id or "").strip())
    except ValueError as e:
        return RedirectResponse(
            "/settings?notified=" + urllib.parse.quote(str(e)), status_code=303)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/notify/{dest_id}/delete")
def notify_delete(dest_id: int):
    notify.delete_destination(dest_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/notify/{dest_id}/enabled")
def notify_enabled(dest_id: int, on: str = Form("1")):
    notify.set_enabled(dest_id, on == "1")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/notify/{dest_id}/test")
def notify_test(dest_id: int):
    """Send a real message. Every integration like this needs one, and nobody
    trusts it without."""
    return RedirectResponse(
        "/settings?notified=" + urllib.parse.quote(notify.test(dest_id)),
        status_code=303)


@router.post("/settings/notify/chatid")
def notify_chatid(dest_id: str = Form(""), token: str = Form("")):
    """Find the Telegram chat id, so nobody hunts for a numeric one by hand.

    The token on the form may be masked, in which case the stored one is used.
    """
    tok = (token or "").strip()
    dest = notify.get_destination(int(dest_id)) if dest_id.strip() else None
    if not tok or tok.startswith("*"):
        tok = (dest or {}).get("token") or ""
    if not tok:
        return RedirectResponse(
            "/settings?notified=" + urllib.parse.quote(
                "Save the bot token first, then press this."), status_code=303)
    found = notify.find_chat_id(tok)
    if not found:
        return RedirectResponse(
            "/settings?notified=" + urllib.parse.quote(
                "Nothing yet. Send the bot a message, then press this again."),
            status_code=303)
    if dest:
        notify.save_destination(
            dest_id=dest["id"], name=dest["name"], kind=dest["kind"],
            events=notify.split_events(dest["events"]),
            remind_hours=dest["remind_hours"], chat_id=found)
    return RedirectResponse(
        "/settings?notified=" + urllib.parse.quote(f"Found chat {found}."),
        status_code=303)
