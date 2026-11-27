"""Settings, for the Apple TV and iPhone apps.

The apps change what the web page changes, through the same `apply`, with a
few things left to the web page on purpose: sign-in, accounts, pairing,
backups, logo uploads, and typing secrets. Each of those can lock somebody
out, replace the whole database, or needs a file or a long key.
"""
import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth, db, demo, handover, language, notify, passes, recorder, sync
from . import settings as web
from ._shared import ZONES, current_user
from .tv import device_for

router = APIRouter()

WEB_ONLY = ("Sign-in, people, televisions, backups, logo uploads and secret keys "
            "are changed on the web page.")


def may_change(request: Request) -> bool:
    """Sign-in off, anyone may, as in a browser. On, a television needs the
    administrator's OK and a person must be an administrator."""
    if auth.mode() == "none":
        return True
    dev = device_for(request)
    if dev:
        return bool(dev["allow_settings"])
    user = current_user(request)
    return bool(user and user.get("role") == "admin")


def _refused():
    return JSONResponse({"ok": False, "error":
                         "This television may not change settings. An administrator "
                         "can allow it in Settings, Televisions, on the web page."},
                        status_code=403)


def _library():
    """The folders and Plex libraries a finished recording can be moved to."""
    out = {"mounted": bool(handover.LIB_DIR) and os.path.isdir(handover.LIB_DIR), "folders": [], "sections": []}
    if out["mounted"]:
        try:
            out["folders"] = sorted(
                n for n in os.listdir(handover.LIB_DIR)
                if os.path.isdir(os.path.join(handover.LIB_DIR, n)) and not n.startswith("."))
        except OSError:
            pass
    try:
        with web.Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            out["sections"] = [
                {"key": str(d.get("key")), "title": d.get("title") or "",
                 "type": d.get("type") or ""}
                for d in plex.library_sections() if d.get("type") in ("show", "movie")]
    except Exception:
        pass
    return out


@router.get("/api/tv/settings")
def read(request: Request):
    s = db.all_settings()
    last = db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
    values = {k: str(s.get(k) or "") for k in web.KEYS if k != "plex_token"}
    if demo.enabled():
        values = demo.scrub_tv_settings(values)
    return {
        "ok": True,
        "may_change": may_change(request),
        "web_only": WEB_ONLY,
        "values": values,
        # Secrets are only ever said to be there or not.
        "plex_token_set": bool(s.get("plex_token")),
        "tmdb_key_set": bool(s.get("tmdb_key")),
        "zones": ZONES,
        "languages": [{"code": k, "name": v[0], "english": v[1]}
                      for k, v in language.LANGUAGES.items()],
        "numbers": {k: list(v) for k, v in web.NUMBERS.items()},
        "recorder": {"available": recorder.available(),
                     "why": recorder.why_unavailable(),
                     "default_now": recorder.default_choice()},
        "library": _library(),
        "last_sync": ({"ended_at": last["ended_at"], "ok": bool(last["ok"]),
                       "detail": last["detail"] or ""} if last else None),
        "notifications": [{"id": d["id"], "name": d.get("name") or d.get("kind") or "",
                           "kind": d.get("kind") or "", "enabled": bool(d.get("enabled"))}
                          for d in notify.destinations()],
    }


@router.post("/api/tv/settings")
async def save(request: Request):
    """Save what was sent, and nothing else."""
    if not may_change(request):
        return _refused()
    form = await request.form()
    sent = {k: form.get(k) for k in web.KEYS
            if k in form and k not in ("plex_token", "announce")}
    refused = web.apply(sent)
    if refused:
        return JSONResponse({"ok": False, "error": "Not saved: " + ", ".join(refused)},
                            status_code=400)
    return {"ok": True, "message": "Saved."}


@router.get("/api/tv/settings/channels")
def channels(request: Request):
    """Every channel with its language: `language` (what it counts as),
    `language_detected` and `language_override` (the person's own choice)."""
    return web.api_channel_languages()


@router.post("/api/tv/settings/channels/{vcn}/language")
async def channel_language(request: Request, vcn: str):
    """`language_override`: a code from `languages`, or empty to go back to the
    detected one."""
    if not may_change(request):
        return _refused()
    form = await request.form()
    return web.channel_language_set(vcn, form.get("language_override", ""))


@router.post("/api/tv/settings/test")
async def test_plex(request: Request):
    if not may_change(request):
        return _refused()
    ok, detail = await asyncio.to_thread(web._test_plex)
    return {"ok": True, "passed": ok, "message": detail}


# Held so a started task is not collected before it ends.
_running = set()


def _start(coro):
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _sync_in_background():
    await asyncio.to_thread(sync.full_sync)
    await asyncio.to_thread(passes.run_passes)


@router.post("/api/tv/settings/sync")
async def sync_now(request: Request):
    """Started, not waited for: a full sync takes longer than an app waits."""
    if not may_change(request):
        return _refused()
    _start(_sync_in_background())
    return {"ok": True, "message": "Sync started. The guide updates in a minute or two."}


@router.post("/api/tv/settings/logos")
async def logos(request: Request):
    if not may_change(request):
        return _refused()
    _start(asyncio.to_thread(sync.cache_logos, True))
    return {"ok": True, "message": "Getting every channel logo again."}


@router.post("/api/tv/settings/notify/{dest_id}/enabled")
async def notify_enabled(request: Request, dest_id: int):
    if not may_change(request):
        return _refused()
    form = await request.form()
    notify.set_enabled(dest_id, form.get("on", "1") == "1")
    return {"ok": True}


@router.post("/api/tv/settings/notify/{dest_id}/test")
async def notify_test(request: Request, dest_id: int):
    if not may_change(request):
        return _refused()
    said = await asyncio.to_thread(notify.test, dest_id)
    # Like Test connection: the apps offer a report only when it did not work.
    # notify.test answers in words, and only a sent test starts with "OK".
    return {"ok": True, "passed": said.startswith("OK"), "message": said}
