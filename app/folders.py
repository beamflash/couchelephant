"""Will a recording be allowed into its Plex folder?

Plex makes a show's folder as its own user, often mode 755. CouchElephant runs
as somebody else, and over NFS root is squashed to somebody else too, so a
folder that looks fine can refuse the copy. The first anyone hears of it would
be a game that never reaches Plex. These checks say so days ahead.

Two kinds of check, on purpose:

* The scan of the whole library (setup, and once a day) only reads: owner,
  group and mode against who we are. Making a file in thousands of folders
  would change every folder's modified time, which makes Plex rescan, and
  would spin up every disk on an Unraid box.
* A real test, making a file and removing it, is for the folder one recording
  is going into: when it is booked and when it starts. That is the folder
  that matters, and one file in it is nothing.

All of it that touches the disk runs on worker threads, never on the recorder
loop or a request, because a network disk that hangs would hang them too. The
lock guards only the small state below and is never held across disk work.

Nothing here changes a permission. Only the folder's owner, or root on the
machine that holds the disk, can. We say which folder and what to run.
"""
import json
import logging
import os
import shlex
import threading
import time

from . import db, health

log = logging.getLogger("couchelephant.folders")

DAY = 86400
# How often the folders already known are looked at again.
RECHECK_SECONDS = 600
# The most one pass over the library may take. A big library on a slow disk
# stops here and is finished tomorrow rather than holding anything up.
SCAN_BUDGET_SECONDS = 300
# How long one folder's real write test may take before it is given up on.
CHECK_SECONDS = 10
# A request that wants the answer waits no longer than this.
REQUEST_WAIT_SECONDS = 3
# Folders that are a NAS's own, not somebody's show.
SKIP = frozenset({"@eaDir", ".snapshots", "#recycle", "@Recycle", "lost+found",
                  "$RECYCLE.BIN", "#snapshot"})
# The unraid host file. A container does not see it unless it is passed in, so
# a miss means "not known", and the text still names Unraid.
UNRAID_FILE = "/etc/unraid-version"
TEST_NAME = ".couchelephant-write-test"
NOBODY = 65534

_lock = threading.Lock()
_booking: dict[int, tuple[str, str]] = {}   # recording id -> (blocked folder, title)
_running = False        # a library pass is going
_again = None           # another was asked for meanwhile: None, False or True (rescan)
_threads: list[threading.Thread] = []
_hung = 0               # real write tests that have not come back
_last_run = 0.0
_no_squash = False      # a real write as root on NFS got through: root is not squashed


# ---------- the checks ----------

def writable(path: str) -> bool:
    """The real test: make a file in the folder and remove it.

    One fixed name, so a file left behind is reused, never added to. If the
    file cannot be removed again the folder is a problem for us, and no more
    are made."""
    if not os.path.isdir(path):
        return False
    name = os.path.join(path, TEST_NAME)
    try:
        os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644))
    except OSError:
        return False
    try:
        os.remove(name)
    except OSError:
        return False
    return True


def _nfs_mounts() -> list[str]:
    try:
        with open("/proc/mounts") as f:
            rows = [line.split() for line in f]
    except OSError:
        return []
    return [r[1].replace("\\040", " ") for r in rows
            if len(r) > 2 and r[2].startswith("nfs")]


def _on_nfs(path: str) -> bool:
    real = os.path.realpath(path)
    best = ""
    kinds = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                r = line.split()
                if len(r) > 2:
                    kinds[r[1].replace("\\040", " ")] = r[2]
    except OSError:
        return False
    for m in kinds:
        if (real == m or real.startswith(m.rstrip("/") + "/")) and len(m) > len(best):
            best = m
    return bool(best) and kinds[best].startswith("nfs")


def _mode_allows(st, uid: int, gids) -> bool:
    """Can this user make and remove entries in a folder with this owner and
    mode: the write bit and the search bit, for the user, group or others."""
    if st.st_uid == uid:
        bits = (st.st_mode >> 6) & 7
    elif st.st_gid in gids:
        bits = (st.st_mode >> 3) & 7
    else:
        bits = st.st_mode & 7
    return bool(bits & 2) and bool(bits & 1)


def quick_writable(path: str) -> bool:
    """Whether a folder looks writable to us, from stat alone. Writes nothing.

    A user that is not root is asked of the system (os.access reads the
    owner, group and mode, and a network disk answers it itself). Root over
    NFS is treated as squashed to "nobody", which is what a NAS does by
    default, unless a real write has shown it is not."""
    try:
        if not os.path.isdir(path):
            return False
        if hasattr(os, "geteuid") and os.geteuid() == 0 and _on_nfs(path) and not _no_squash:
            return _mode_allows(os.stat(path), NOBODY, {NOBODY})
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def nearest_existing(path: str) -> str | None:
    """The folder itself, or the closest parent that exists."""
    while path and not os.path.isdir(path):
        up = os.path.dirname(path)
        if up == path:
            return None
        path = up
    return path or None


# ---------- where things are ----------

def _root_settings() -> dict[str, str]:
    """The TV and movies roots as this computer sees them. No disk access."""
    from . import handover
    out = {}
    for k in ("shows", "movies"):
        folder = (db.get_setting(f"recorder_{k}_folder") or "").strip()
        if not folder:
            continue
        if handover.LIB_DIR:
            folder = os.path.join(handover.LIB_DIR, folder.strip("/"))
        elif not os.path.isabs(folder):
            continue
        out[k] = folder
    return out


def library_roots() -> list[str]:
    out = []
    for folder in _root_settings().values():
        if os.path.isdir(folder) and folder not in out:
            out.append(folder)
    return out


def _subdirs(path: str) -> list[str]:
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if not n.startswith(".") and n not in SKIP
                and os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def _plex_roots() -> dict:
    try:
        return json.loads(db.get_setting("folders_plex_roots") or "{}")
    except ValueError:
        return {}


def _learn_plex_roots() -> None:
    """Where Plex says each library folder is. That is the path to show for
    the fix: this container's /library does not exist on the NAS."""
    from .plex import Plex
    found = {}
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            sections = {str(d.get("key")): d for d in plex.library_sections()}
    except Exception as e:  # noqa: BLE001
        log.info("Plex's library folders not read: %s", e)
        return
    for k in ("shows", "movies"):
        d = sections.get((db.get_setting(f"recorder_{k}_section") or "").strip())
        places = (d or {}).get("Location") or []
        if len(places) == 1 and places[0].get("path"):
            found[k] = places[0]["path"].rstrip("/")
    db.set_setting("folders_plex_roots", json.dumps(found))


def describe(folder: str) -> dict:
    """How to name a folder to a person, and the command that fixes it.

    The container's own path means nothing on the machine that holds the
    library, so the folder is named by its library and place in it, and the
    command uses Plex's own path for it when that is known."""
    from . import handover
    kind = root = None
    for k, r in _root_settings().items():
        if folder == r or folder.startswith(r.rstrip("/") + "/"):
            kind, root = k, r
            break
    if not kind:
        return {"label": f'the folder {folder}', "root": False,
                "cmd": f"chmod -R a+rwX {shlex.quote(folder)}"}
    word = "TV" if kind == "shows" else "movies"
    plex = _plex_roots().get(kind)
    if not handover.LIB_DIR:
        plex = root                 # a native install sees the real path
    if folder == root:
        cmd = (f"chmod a+rwx {shlex.quote(plex)}" if plex else
               f"chmod a+rwx on your {word} library folder itself, and only that folder")
        return {"label": f"the {word} library folder", "root": True, "cmd": cmd}
    sub = os.path.relpath(folder, root)
    if plex:
        cmd = f"chmod -R a+rwX {shlex.quote(plex + '/' + sub)}"
    else:
        cmd = (f"cd to your {word} library folder, then: "
               f"chmod -R a+rwX {shlex.quote(sub)}")
    return {"label": f'the folder "{sub}" in the {word} library', "root": False,
            "cmd": cmd}


def is_unraid() -> bool:
    return os.path.exists(UNRAID_FILE)


def unraid_note() -> str:
    if is_unraid():
        return ("This is Unraid: open Tools, New Permissions, pick the share "
                "and run it. That sets the folders right for every user.")
    return ("On Unraid, use Tools, New Permissions on the share instead. "
            "That sets the folders right for every user.")


def cannot_write(folder: str) -> str:
    return f"CouchElephant cannot write to {describe(folder)['label']}."


def fix_command(blocked: list[str]) -> str:
    """What to run, and where: on the machine that holds the library."""
    ds = [describe(f) for f in blocked]
    text = "On the machine that holds the library, run: " + " ; ".join(d["cmd"] for d in ds)
    if any(d["root"] for d in ds):
        text += (". That is the library folder itself, so do not use -R on all of it. "
                 "Make the show's folder there yourself, or fix only that folder's own mode")
    return text + "."


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
    Plex makes, now and later."""
    seen: dict[tuple[int, int], int] = {}
    for root in library_roots():
        for p in _subdirs(root)[:50]:
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


# ---------- the scan: reads only ----------

def _scan(budget: float) -> tuple[list[str], bool]:
    """Every show, movie and season folder, one level under each root and the
    season level below a show, that we cannot write. Stat only. Stops at the
    budget and says it did not finish."""
    global _no_squash
    end = time.monotonic() + budget
    roots = _root_settings()
    # Root over NFS: one real write in the library's own folder says whether
    # the NAS squashes root. Only then is root believed.
    _no_squash = False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for r in roots.values():
            if os.path.isdir(r) and _on_nfs(r):
                if not _mode_allows(os.stat(r), NOBODY, {NOBODY}) and writable(r):
                    _no_squash = True
                break
    blocked = []
    for kind, root in roots.items():
        if not os.path.isdir(root):
            continue
        for d in _subdirs(root):
            if time.monotonic() > end:
                return blocked, False
            if not quick_writable(d):
                blocked.append(d)
            if kind == "shows":
                for season in _subdirs(d):
                    if not quick_writable(season):
                        blocked.append(season)
    return blocked, True


def scan() -> list[str]:
    return _scan(SCAN_BUDGET_SECONDS)[0]


def _stored() -> dict:
    try:
        return json.loads(db.get_setting("folders_scan") or "{}")
    except ValueError:
        return {}


# ---------- one recording's folder: the real test ----------

def _active_recordings():
    return db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('scheduled', 'recording', 'finished', 'moving')")


def _blocked_for(rec, real: bool, cache: dict | None = None) -> str | None:
    """The folder that stops this recording, or None."""
    from . import handover
    d = handover.target_dir(rec, cache)
    if not d:
        return None
    near = nearest_existing(d)
    if not near:
        return None
    ok = writable(near) if real else quick_writable(near)
    return None if ok else near


def _timed(fn, seconds: float):
    """fn() on its own thread, waited for at most `seconds`. None if it did
    not come back. A few that never do are all that are ever left waiting."""
    global _hung
    with _lock:
        if _hung >= 4:
            return None
        _hung += 1
    box = []
    done = threading.Event()

    def run():
        global _hung
        try:
            box.append(fn())
        except Exception as e:  # noqa: BLE001
            log.info("folder check failed: %s", e)
            box.append(None)
        finally:
            with _lock:
                _hung -= 1
            done.set()
    threading.Thread(target=run, daemon=True, name="ce-folder-test").start()
    return box[0] if done.wait(seconds) and box else None


def booking_warning(rec) -> str | None:
    """A sentence for a booking that goes into a folder we cannot write. Waits
    a few seconds at most, and says nothing if the disk does not answer."""
    near = _timed(lambda: _blocked_for(rec, True), REQUEST_WAIT_SECONDS)
    if not near:
        return None
    with _lock:
        _booking[rec["id"]] = (near, rec["title"] or "")
    return f"{cannot_write(near)} {fix_command([near])}"


def check_recording(rec_id: int) -> None:
    rec = db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))
    if not rec or rec["state"] not in ("scheduled", "recording", "finished", "moving"):
        with _lock:
            _booking.pop(rec_id, None)
        publish()
        return
    near = _timed(lambda: _blocked_for(rec, True), CHECK_SECONDS)
    with _lock:
        if near:
            _booking[rec_id] = (near, rec["title"] or rec["show_title"] or "")
        else:
            _booking.pop(rec_id, None)
    publish()


# ---------- the one notice ----------

def publish(now: int | None = None) -> None:
    """Raise or clear the notice from what is known. Database only: no disk."""
    now = now or int(time.time())
    states = {r["id"]: r["state"] for r in _active_recordings()}
    with _lock:
        for rid in [i for i in _booking if i not in states]:
            del _booking[rid]
        mine = dict(_booking)
    by_folder: dict[str, list[str]] = {}
    red = False
    for rid, (folder, title) in mine.items():
        by_folder.setdefault(folder, []).append(title or "A recording")
        if states.get(rid) != "scheduled":
            red = True
    known = [f for f in _stored().get("blocked", [])]
    blocked = sorted(set(known) | set(by_folder))
    if not blocked:
        health.record([], now, owns=health.FOLDER_CODES)
        return
    health.record([_notice(blocked, by_folder, red)], now, owns=health.FOLDER_CODES)


def _notice(blocked, by_folder, red) -> dict:
    lines = [cannot_write(blocked[0]) if len(blocked) == 1 else
             "CouchElephant cannot write to: " + "; ".join(
                 describe(f)["label"] for f in blocked) + "."]
    for folder, names in sorted(by_folder.items()):
        lines.append(f"{', '.join(sorted(set(names)))} goes into "
                     f"{describe(folder)['label']}.")
    lines.append(fix_command(blocked))
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


# ---------- a whole pass: setup, daily, and every few minutes ----------

def refresh(now: int | None = None, rescan: bool = False) -> None:
    """A full pass, on whatever thread calls it. Reads the library when
    `rescan`, looks again at what is known blocked, looks at every active
    recording, and publishes. Never called from the recorder loop or a
    request: use `rescan_soon` or `tick`."""
    global _last_run
    now = now or int(time.time())
    _last_run = time.time()
    if not _root_settings() or not library_roots():
        # Nothing to be blocked from: clear what an old library left open.
        db.set_setting("folders_scan", json.dumps({"at": now, "blocked": []}))
        with _lock:
            _booking.clear()
        publish(now)
        return
    if rescan:
        _learn_plex_roots()
        found, whole = _scan(SCAN_BUDGET_SECONDS)
        # An unfinished pass keeps what the last one found beyond its reach.
        if not whole:
            found = sorted(set(found) | set(_stored().get("blocked", [])))
        db.set_setting("folders_scan", json.dumps({"at": now, "blocked": found}))
    else:
        still = [f for f in _stored().get("blocked", []) if not quick_writable(f)]
        db.set_setting("folders_scan", json.dumps(
            {"at": _stored().get("at") or now, "blocked": still}))
    cache: dict = {}        # the library is listed once, not per recording
    for rec in _active_recordings():
        with _lock:
            known = rec["id"] in _booking
        near = _blocked_for(rec, real=known, cache=cache)
        with _lock:
            if near:
                _booking[rec["id"]] = (near, rec["title"] or rec["show_title"] or "")
            else:
                _booking.pop(rec["id"], None)
    publish(now)


def _spawn(fn, *args) -> None:
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        if len(_threads) >= 8:
            return              # a disk that hangs must not pile up threads
        t = threading.Thread(target=fn, args=args, daemon=True, name="ce-folders")
        _threads.append(t)
    t.start()


def _pass(rescan: bool) -> None:
    global _running, _again
    while True:
        try:
            refresh(rescan=rescan)
        except Exception as e:  # noqa: BLE001
            log.warning("folder check failed: %s", e)
        with _lock:
            if _again is None:
                _running = False
                return
            rescan, _again = _again, None


def rescan_soon(rescan: bool = True) -> None:
    """Ask for a library pass on its own thread. One at a time: a second
    request while one is going is run once more after it, not alongside."""
    global _running, _again
    with _lock:
        if _running:
            _again = bool(rescan or _again)
            return
        _running = True
    _spawn(_pass, rescan)


def tick(now: int | None = None) -> None:
    """From the recorder's loop, so it only decides and starts a thread: the
    daily scan, and a recheck every few minutes so a fixed folder clears."""
    now = now or int(time.time())
    if now - int(_stored().get("at") or 0) >= DAY:
        rescan_soon(True)
    elif time.time() - _last_run >= RECHECK_SECONDS:
        rescan_soon(False)


def settle(seconds: float = 20) -> None:
    """Wait for the worker threads. For tests."""
    end = time.time() + seconds
    while time.time() < end:
        with _lock:
            alive = [t for t in _threads if t.is_alive()]
            idle = not _running
        if not alive and idle:
            return
        time.sleep(0.02)


def on_booked(rec_id: int) -> None:
    """A recording was booked. Its own folder is checked on a worker thread,
    so booking never waits on the disk."""
    _spawn(check_recording, rec_id)


def on_start(rec) -> None:
    """The recording has started: this is no longer a risk, it is a problem.
    Called on a thread of its own."""
    try:
        check_recording(rec["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("folder check at the start failed: %s", e)
