"""What an issue report needs from this server: its scrubbed part, and the
Plex account email to prefill the contact line. Administrators and paired
devices only (the gate in web.py); neither answer carries a token."""
import asyncio
import logging

from fastapi import APIRouter

from .. import db, plextv, reportbuild

router = APIRouter()
log = logging.getLogger(__name__)

DEADLINE = 5.0   # seconds; an app that times out still sends its own part


def _build():
    return reportbuild.server_part(reportbuild.make_scrubber())


@router.get("/api/report/server")
async def report_server():
    try:
        part = await asyncio.wait_for(asyncio.to_thread(_build), DEADLINE)
    except TimeoutError:
        return {"ok": True, "server": None, "note": "server part timed out"}
    return {"ok": True, "server": part}


@router.get("/api/report/email")
async def report_email():
    token = db.get_setting("plex_token")
    email = await asyncio.to_thread(plextv.account_email, token) if token else None
    return {"ok": True, "email": email or ""}
