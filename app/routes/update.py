"""Is there a newer release, and the one-click update."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth, updates
from ._shared import current_user

router = APIRouter()


def _is_admin(request: Request) -> bool:
    if auth.mode() == "none":
        return True               # no accounts: whoever is here runs the install
    user = current_user(request)
    return bool(user and user.get("role") == "admin")


@router.get("/api/update")
def api_update(request: Request):
    data = updates.status()
    if not _is_admin(request):
        # Only administrators are told; the rest just see the app.
        data.update(latest=None, url=None, click=False)
    return JSONResponse(data)


@router.post("/api/update/apply")
async def api_update_apply(request: Request):
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "administrators only"}, status_code=403)
    try:
        message = await asyncio.to_thread(updates.apply)
    except updates.UpdateError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    except Exception as err:
        return JSONResponse({"ok": False, "error": f"The update failed: {err}"},
                            status_code=500)
    return JSONResponse({"ok": True, "message": message})
