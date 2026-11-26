"""The title card shown for a recording that has no picture of its own."""
from fastapi import APIRouter
from fastapi.responses import Response

from .. import db, recart

router = APIRouter()


def _words(key):
    """The name to draw for a card key, from the recording that owns it."""
    rec = db.one("SELECT title, show_title FROM recordings WHERE uid = ?", (key,))
    if rec:
        return rec["show_title"] or rec["title"] or "Recording", \
            rec["title"] if rec["show_title"] and rec["title"] != rec["show_title"] else ""
    if key.startswith("plex-"):
        g = db.one("SELECT title, parent_title FROM plex_grabs WHERE id = ?", (key[5:],))
        if g:
            return g["parent_title"] or g["title"] or "Recording", \
                g["title"] if g["parent_title"] else ""
    return None


@router.get("/card/{key}/{sig}.png")
def card(key: str, sig: str):
    """Open on purpose: a television's home-screen image load sends no key.
    The address is only valid with the server's own signature for this key, so
    it cannot be guessed, and the picture says nothing the list did not."""
    if not recart.signature_ok(key, sig):
        return Response(status_code=404)
    words = _words(key)
    if not words:
        return Response(status_code=404)
    return Response(recart.render(*words), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})
