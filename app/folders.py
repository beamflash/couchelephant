"""Will a recording be allowed into its Plex folder?

Plex makes a show's folder as its own user, often mode 755. CouchElephant runs
as somebody else, and over NFS root is squashed to somebody else too, so a
folder that looks fine can refuse the copy. The first anyone hears of it would
be a game that never reaches Plex. These checks say so days ahead.

The test is a real one: make a file in the folder and remove it. os.access
answers from the mode bits, and those lie over NFS.

Nothing here changes a permission. Only the folder's owner, or root on the
machine that holds the disk, can. We say which folder and what to run.
"""
import json
import logging
import os
import tempfile
import threading
import time

from . import db, health

log = logging.getLogger("couchelephant.folders")

DAY = 86400
# Where the folders already known to be blocked are checked again.
RECHECK_SECONDS = 600
# The unraid host file. A container does not see it unless it is passed in, so
# a miss means "not known", and the text still names Unraid.
UNRAID_FILE = "/etc/unraid-version"

_lock = threading.Lock()
_rechecked = 0.0


# ---------- the write test ----------

def writable(path: str) -> bool:
    """Can a file be made in this folder, and removed again."""
    if not os.path.isdir(path):
        return False
    try:
        fd, name = tempfile.mkstemp(dir=path, prefix=".couchelephant-check-")
    except OSError:
        return False
    os.close(fd)
    try:
        os.remove(name)
    except OSError:
        pass            # we could write; a leftover dot file does not block a copy
    return True


def nearest_existing(path: str) -> str | None:
    """The folder itself, or the closest parent that exists."""
    while path and not os.path.isdir(path):
        up = os.path.dirname(path)
        if up == path:
            return None
        path = up
    return path or None


# ---------- the words ----------

def is_unraid() -> bool:
    return os.path.exists(UNRAID_FILE)


def unraid_note() -> str:
    if is_unraid():
        return ("This is Unraid: open Tools, New Permissions, pick the share "
                "and run it. That sets the folders right for every user.")
    return ("On Unraid, use Tools, New Permissions on the share instead. "
            "That sets the folders right for every user.")


def fix_command(blocked: list[str]) -> str:
    """One command per folder, to copy as it is."""
    return " ; ".join(f'chmod -R a+rwX "{f}"' for f in blocked)


def cannot_write(folder: str) -> str:
    return f"CouchElephant cannot write to the folder {folder}."


# ---------- which user ----------

def _owner(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_uid, st.st_gid


def _me() -> tuple[int, int]:
    if hasattr(os, "geteuid"):
        return os.geteuid(), os.getegid()
    return -1, -1


def user_advice() -> str | None:
    """Which user made the library's folders, and to run as that user.

    The user that owns the show folders Plex made is the one Plex runs as. If
    CouchElephant is another user, say so: the same user can write whatever
    Plex makes, now and later.
    """
    seen: dict[tuple[int, int], int] = {}
    for root in library_roots():
        try:
            names = sorted(os.listdir(root))[:50]
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if n.startswith(".") or not os.path.isdir(p):
                continue
            o = _owner(p)
            if o:
                seen[o] = seen.get(o, 0) + 1
    if not seen:
        return None
    (uid, gid), _ = max(seen.items(), key=lambda kv: kv[1])
    if (uid, gid) == _me() or uid == 0:
        return None
    return (f"Plex made its folders as user {uid}, group {gid}. Run CouchElephant "
            f"as the same user and it can write whatever Plex makes: set PUID={uid} "
            f"and PGID={gid} on the container, then recreate it.")


# ---------- what is checked ----------

def library_roots() -> list[str]:
    """The TV and movies folders, as this computer sees them."""
    from . import handover
    out = []
    for k in ("shows", "movies"):
        folder = (db.get_setting(f"recorder_{k}_folder") or "").strip()
        if not folder:
            continue
        if handover.LIB_DIR:
            folder = os.path.join(handover.LIB_DIR, folder.strip("/"))
        elif not os.path.isabs(folder):
            continue
        if os.path.isdir(folder) and folder not in out:
            out.append(folder)
    return out


def _subdirs(path: str) -> list[str]:
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if not n.startswith(".") and os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def scan() -> list[str]:
    """Every show, movie and season folder, one level under each root and the
    season level below a show, that cannot be written."""
    from . import handover
    blocked = []
    shows = None
    folder = (db.get_setting("recorder_shows_folder") or "").strip()
    if folder:
        shows = os.path.join(handover.LIB_DIR, folder.strip("/")) if handover.LIB_DIR else folder
    for root in library_roots():
        for d in _subdirs(root):
            if not writable(d):
                blocked.append(d)
            if root == shows:
                for season in _subdirs(d):
                    if not writable(season):
                        blocked.append(season)
    return blocked


def _stored() -> dict:
    try:
        return json.loads(db.get_setting("folders_scan") or "{}")
    except ValueError:
        return {}


def _active_recordings():
    return db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('scheduled', 'recording', 'finished', 'moving')")


def _blocked_for(rec) -> str | None:
    """The folder that stops this recording, or None."""
    from . import handover
    d = handover.target_dir(rec)
    if not d:
        return None
    near = nearest_existing(d)
    if near and not writable(near):
        return near
    return None


def booking_warning(rec) -> str | None:
    """A sentence for a booking that goes into a folder we cannot write."""
    near = _blocked_for(rec)
    if not near:
        return None
    return (f"{cannot_write(near)} This recording will stay in CouchElephant "
            f"until that is fixed. Run: {fix_command([near])}")


def refresh(now: int | None = None, rescan: bool = False) -> None:
    """Work out the one notice from what is known now, and raise or clear it.

    Folders found by the last scan are checked again here, so a folder fixed
    since then stops being reported. `rescan` walks the library afresh.
    """
    now = now or int(time.time())
    with _lock:
        if rescan:
            found = scan() if library_roots() else []
            db.set_setting("folders_scan", json.dumps({"at": now, "blocked": found}))
        known = [f for f in _stored().get("blocked", []) if not writable(f)]
        stuck, waiting = [], []     # recordings that need it now, and later
        by_folder: dict[str, list] = {}
        for rec in _active_recordings():
            near = _blocked_for(rec)
            if near:
                by_folder.setdefault(near, []).append(rec)
                (waiting if rec["state"] == "scheduled" else stuck).append(rec)
        blocked = sorted(set(known) | set(by_folder))
        if not blocked:
            health.record([], now, owns=health.FOLDER_CODES)
            return
        health.record([_notice(blocked, by_folder, bool(stuck))], now,
                      owns=health.FOLDER_CODES)


def _notice(blocked, by_folder, red) -> dict:
    lines = [f"{cannot_write(', '.join(blocked))}"
             if len(blocked) == 1 else
             "CouchElephant cannot write to these folders: " + "; ".join(blocked) + "."]
    for folder, recs in sorted(by_folder.items()):
        names = ", ".join(sorted({r["title"] or r["show_title"] or "A recording" for r in recs}))
        lines.append(f"{names} goes into {folder}.")
    lines.append("Run: " + fix_command(blocked))
    hint = unraid_note() + " CouchElephant never changes permissions itself."
    advice = user_advice()
    if advice:
        hint += " " + advice
    return {
        "code": health.FOLDER_BLOCKED,
        "severity": "bad" if red else "warn",
        "title": ("A recording cannot be saved to Plex"
                  if red else "A Plex library folder will block a recording"),
        "detail": " ".join(lines),
        "hint": hint,
    }


# ---------- when it runs ----------

def tick(now: int | None = None) -> None:
    """From the recorder's loop: the daily scan, and a recheck every few
    minutes so a fixed folder clears its notice."""
    global _rechecked
    now = now or int(time.time())
    if not library_roots():
        return
    if now - int(_stored().get("at") or 0) >= DAY:
        refresh(now, rescan=True)
        _rechecked = time.time()
    elif time.time() - _rechecked >= RECHECK_SECONDS:
        _rechecked = time.time()
        refresh(now)


def on_booked(rec_id: int) -> None:
    """A recording was booked. Say so days ahead, not on the day."""
    try:
        refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("folder check after booking failed: %s", e)


def on_start(rec) -> None:
    """The recording has started: this is no longer a risk, it is a problem."""
    try:
        refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("folder check at the start failed: %s", e)
