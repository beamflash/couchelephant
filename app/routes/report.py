"""What an issue report needs from this server: its scrubbed part, and the
Plex account email to prefill the contact line. Those two reads are for
administrators and paired devices only (the gate in web.py); neither carries
a token. Consent and send are open to any signed-in person."""
import asyncio
import logging
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import db, demo, plextv, reportbuild, reportsend
from ..reportscrub import cut_take
from ..version import VERSION

router = APIRouter()
log = logging.getLogger(__name__)

DEADLINE = 5.0   # seconds each; an app that times out still sends its own part


def _build():
    return reportbuild.server_part(reportbuild.make_scrubber())


@router.get("/api/report/server")
async def report_server():
    try:
        part = await asyncio.wait_for(asyncio.to_thread(_build), DEADLINE)
    except TimeoutError:
        return {"ok": True, "server": None, "note": "server part timed out"}
    except Exception as e:
        log.warning("report server part failed (%s)", type(e).__name__)
        return {"ok": True, "server": None, "note": "server part failed"}
    return {"ok": True, "server": part}


@router.get("/api/report/email")
async def report_email():
    token = db.get_setting("plex_token")
    if not token:
        return {"ok": True, "email": ""}
    try:
        email = await asyncio.wait_for(
            asyncio.to_thread(plextv.account_email, token), DEADLINE)
    except TimeoutError:
        email = None
    return {"ok": True, "email": email or ""}


# ---- consent and send (any signed-in person) ----

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
MESSAGE_MAX = 20_000
KINDS = ("error", "support", "crash")
SAVED_COPY = "Could not send. It is saved and will be sent later."
LIMIT_COPY = "Too many reports today. Try again tomorrow."
NOT_SENT = {
    "limit": LIMIT_COPY,
    "too_large": "This report is too large to send.",
    "not_configured": "Reports are not set up on this server.",
    "rejected": "The report could not be sent.",
    "demo": demo.REPORT_NOTE,
}


def _bad(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# How much of a line is read, and how much is kept once it is scrubbed.
LINE_TAKE = 8000
LINE_KEEP = 2000


def _str(v, cap):
    return v[:cap] if isinstance(v, str) else ""


@router.get("/api/report/consent")
async def consent_state():
    return {"ok": True, "consented": db.get_setting("report_consent") == "1",
            "queued": reportsend.queue_size()}


@router.post("/api/report/consent")
async def consent_give(agree: str = Form("")):
    if agree != "1":
        return _bad("agree is required")
    db.set_setting("report_consent", "1")
    return {"ok": True}


@router.post("/api/report/send")
async def report_send(request: Request):
    if db.get_setting("report_consent") != "1":
        return _bad("consent", 403)
    try:
        body = await request.json()
    except Exception:
        return _bad("bad request")
    if not isinstance(body, dict):
        return _bad("bad request")
    message = body.get("message") or ""
    email = body.get("email") or ""
    if not isinstance(message, str) or not isinstance(email, str):
        return _bad("bad request")
    if len(message) > MESSAGE_MAX:
        return _bad("That message is too long.")
    email = email.strip()
    if email and (len(email) > 254 or not EMAIL_RE.match(email)):
        return _bad("That email address does not look right.")
    trig = body.get("trigger")
    trig = trig if isinstance(trig, dict) else {}
    if trig.get("kind") not in KINDS:
        return _bad("bad trigger")
    trigger = {"kind": trig["kind"]}
    for k in ("error", "screen"):
        if isinstance(trig.get(k), str):
            trigger[k] = cut_take(trig[k], LINE_TAKE)
    client = body.get("client")
    client = client if isinstance(client, dict) else {}
    app = {"version": VERSION, "platform": "web",
           "ua": _str(client.get("ua"), 500),
           "screen_size": _str(client.get("screen_size"), 50)}
    lines = body.get("app_log")
    app_log = [cut_take(x, LINE_TAKE) for x in lines[-500:] if isinstance(x, str)] \
        if isinstance(lines, list) else []

    scrub = reportbuild.make_scrubber()
    # Scrub first, then cap: a cut first would leave half a secret that no
    # rule can recognise.
    for k in ("error", "screen"):
        if k in trigger:
            trigger[k] = scrub.text(trigger[k])[:LINE_KEEP]
    app_log = [scrub.text(x)[:LINE_KEEP] for x in app_log]
    server, note = None, None
    try:
        server = await asyncio.wait_for(
            asyncio.to_thread(reportbuild.server_part, scrub), DEADLINE)
    except TimeoutError:
        note = "server part timed out"
    except Exception as e:
        log.warning("report server part failed (%s)", type(e).__name__)
        note = "server part failed"
    report = reportbuild.build_report(
        source="web", app=app, trigger=trigger, app_log=app_log, server=server,
        server_note=note, message=message, email=email, scrub=scrub)
    res = await asyncio.to_thread(reportsend.submit, report, "web")
    if not res["ok"]:
        return {"ok": False, "error": NOT_SENT.get(res["reason"], NOT_SENT["rejected"])}
    return {"ok": True, "sent": res["sent"], "queued": res["queued"],
            "reportId": res["report_id"],
            "message": "Sent. Thank you." if res["sent"] else SAVED_COPY}
