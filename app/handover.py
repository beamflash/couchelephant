"""Giving a finished recording to Plex.

The broadcast file is copied into the Plex library under the name Plex would
have given it, Plex is asked to scan, and only when Plex lists the file is the
held copy removed. Nothing is deleted on a promise.

The viewing copy is never touched here, so a recording stays playable in
CouchElephant before, during and after the move.
"""
import datetime
import logging
import os
import re
import shutil
import threading
import time
import zoneinfo

import httpx

from . import db, folders, paths, recorder
from .plex import Plex, PlexError

log = logging.getLogger("couchelephant.handover")

# None on a native install: the configured folders are absolute paths.
_lib = paths.library_dir()
LIB_DIR = str(_lib) if _lib else None
# A handover that failed is tried again after this long.
RETRY_SECONDS = 600
# How long one attempt waits for Plex to list the file it was asked to scan.
SCAN_WAIT_SECONDS = 180
SCAN_POLL_SECONDS = 5

_worker: threading.Thread | None = None
_lock = threading.Lock()


class HandoverError(RuntimeError):
    """A sentence a person can read."""


# ---------- is it set up ----------

def _kind(rec) -> str:
    return "movies" if rec["ptype"] == "movie" else "shows"


def why_unavailable(rec=None) -> str | None:
    """None when a recording can be handed over, or what is missing."""
    if LIB_DIR and not os.path.isdir(LIB_DIR):
        return (f"There is no {LIB_DIR} folder. Mount the Plex library there to "
                "have recordings moved into Plex.")
    kinds = [_kind(rec)] if rec is not None else ["shows"]
    for k in kinds:
        word = "movies" if k == "movies" else "TV"
        folder = (db.get_setting(f"recorder_{k}_folder") or "").strip()
        if not LIB_DIR and folder and not os.path.isabs(folder):
            return (f"The {word} library folder needs its full path, such as "
                    "/home/you/Movies. Choose it in Settings, Recording, CouchElephant DVR.")
        if not (db.get_setting(f"recorder_{k}_folder") or "").strip():
            return f"No library folder is chosen for {word}. Choose one in Settings, Recording, CouchElephant DVR."
        if not (db.get_setting(f"recorder_{k}_section") or "").strip():
            return f"No Plex library is chosen for {word}. Choose one in Settings, Recording, CouchElephant DVR."
    return None


# ---------- the name Plex is given ----------

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(name: str) -> str:
    """A name every filesystem accepts. Never empty."""
    out = _BAD.sub(" ", name or "")
    out = re.sub(r"\s+", " ", out).strip().rstrip(".")
    return out or "Untitled"


def _zone():
    try:
        return zoneinfo.ZoneInfo(db.get_setting("timezone") or "UTC")
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def _listdir(path: str, cache: dict | None) -> list[str]:
    """os.listdir, remembered in `cache` when one is given. A library of
    thousands of folders is listed once per check, not once per recording."""
    if cache is None:
        return os.listdir(path)
    if path not in cache:
        cache[path] = os.listdir(path)
    return cache[path]


def _existing(root: str, show: str, cache: dict | None = None) -> str:
    """The folder this show already has in the library, or the plain name.

    Plex names a show folder `Show (Year)`, and the guide does not say the
    show's year. Using the folder that is already there puts the recording
    next to the others instead of starting a second show.
    """
    want = show.lower()
    try:
        names = _listdir(root, cache)
    except OSError:
        return show
    for n in sorted(names):
        low = n.lower()
        if low == want or re.fullmatch(re.escape(want) + r" \(\d{4}\)", low):
            if os.path.isdir(os.path.join(root, n)):
                return n
    return show


def _season_folder(root: str, show_folder: str, n: int, cache: dict | None = None) -> str:
    """The season folder the show already has on disk ("Season 1" as well as
    "Season 01"), or the plain two digit name. With both, the one with more in
    it, which is the one Plex is reading."""
    best, size = None, -1
    try:
        for name in sorted(_listdir(os.path.join(root, show_folder), cache), reverse=True):
            path = os.path.join(root, show_folder, name)
            if re.fullmatch(rf"season 0*{n}", name.lower()) and os.path.isdir(path):
                count = len(_listdir(path, cache))
                if count > size:   # a tie goes to "Season 1" over "Season 01"
                    best, size = name, count
    except OSError:
        pass
    return best or f"Season {n:02d}"


def relative_path(rec, root: str | None = None, number=None, cache=None) -> str:
    """Where the file goes, under the TV or movies folder.

    `number` is (season, episode) as Plex numbers it, when that is known and
    differs from the guide's."""
    title = clean(rec["title"])
    if rec["ptype"] == "movie":
        name = f"{title} ({rec['year']})" if rec["year"] else title
        return os.path.join(name, name + ".ts")
    show = clean(rec["show_title"] or rec["title"])
    folder = _existing(root, show, cache) if root else show
    if rec["season"] is not None and rec["episode"] is not None:
        sn, ep = number or (int(rec["season"]), int(rec["episode"]))
        season = _season_folder(root, folder, sn, cache) if root else f"Season {sn:02d}"
        name = f"{folder} - S{sn:02d}E{ep:02d}"
    else:
        # Sport, news, anything the guide gives no number: by the day it aired.
        day = datetime.datetime.fromtimestamp(rec["begins_at"], _zone())
        season = f"Season {day.year}"
        name = f"{folder} - {day:%Y-%m-%d}"
    if title and title.lower() != show.lower():
        name += f" - {title}"
    return os.path.join(folder, season, name + ".ts")


# ---------- the move ----------

def _plex_folder(plex: Plex, section: str, rel_dir: str) -> str | None:
    """The folder as Plex sees it, so the scan can be for it alone.

    Only when the library reads one folder. With several there is no telling
    which one the mount is, and the whole library is scanned instead.
    """
    for d in plex.library_sections():
        if str(d.get("key")) == str(section):
            places = d.get("Location") or []
            if len(places) == 1 and places[0].get("path"):
                return places[0]["path"].rstrip("/") + "/" + rel_dir
    return None


def _make_folders(path: str) -> None:
    """Make the missing folders open to Plex. The Linux service's umask
    keeps our own data private, and would hide these from Plex too."""
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for d in reversed(missing):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o755)


def _copy(src: str, dest: str) -> None:
    size = os.path.getsize(src)
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return                      # an earlier attempt got this far
    _make_folders(os.path.dirname(dest))
    part = dest + ".part"           # an ending Plex does not read
    shutil.copyfile(src, part)
    os.chmod(part, 0o644)           # Plex runs as its own user
    if os.path.getsize(part) != size:
        os.remove(part)
        raise HandoverError("The copy into the Plex library came out the wrong size.")
    os.replace(part, dest)


def target_dir(rec, cache: dict | None = None) -> str | None:
    """The folder this recording goes into, or None when no handover is set up.

    It may not exist yet. The path is worked out the way the copy works it out."""
    if why_unavailable(rec):
        return None
    k = _kind(rec)
    folder = db.get_setting(f"recorder_{k}_folder").strip()
    if LIB_DIR:
        folder = folder.strip("/")
    root = os.path.join(LIB_DIR or "", folder)
    old = rec["plex_path"]
    if old and not os.path.relpath(old, root).startswith(".."):
        return os.path.dirname(old)
    return os.path.dirname(os.path.join(root, relative_path(rec, root, cache=cache)))


def hand_over(rec) -> None:
    """Copy, scan, wait, carry the position, then let go of our copy.

    Raises HandoverError with the reason. On any failure the held broadcast
    file is still here.
    """
    why = why_unavailable(rec)
    if why:
        raise HandoverError(why)
    k = _kind(rec)
    folder = db.get_setting(f"recorder_{k}_folder").strip()
    if LIB_DIR:
        folder = folder.strip("/")      # a name inside the mount, not a path
    section = db.get_setting(f"recorder_{k}_section").strip()
    root = os.path.join(LIB_DIR or "", folder)
    src = recorder.broadcast_path(rec["uid"])
    if not os.path.exists(src):
        raise HandoverError("The recorded file is missing.")
    # Ask Plex how it numbers this episode: the guide is sometimes one off.
    number = None
    if k == "shows" and rec["season"] is not None and rec["episode"] is not None:
        try:
            with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
                number = plex.episode_numbering(section, rec["show_title"] or rec["title"],
                                                rec["title"])
        except Exception:  # noqa: BLE001
            number = None           # Plex not answering is not a reason to stop
    old = rec["plex_path"]
    old_rel = old and os.path.relpath(old, root)
    if old_rel and old_rel.startswith(".."):
        old_rel = None
    if old_rel and number is None:
        rel = old_rel               # the same place as the last attempt
    else:
        rel = relative_path(rec, root, number)
    dest = os.path.join(root, rel)
    moved_from = None
    if old_rel and os.path.join(root, old_rel) != dest:
        # Put under the wrong number before. Move our own file, no other.
        prev = os.path.join(root, old_rel)
        if prev.endswith(".ts") and os.path.isfile(prev) and not os.path.exists(dest):
            _make_folders(os.path.dirname(dest))
            os.replace(prev, dest)
            moved_from = os.path.dirname(old_rel)
            try:
                os.rmdir(os.path.dirname(prev))
            except OSError:
                pass
    recorder._set(rec["id"], state="moving", plex_path=dest, error=None)
    try:
        _copy(src, dest)
    except OSError as e:
        near = folders.nearest_existing(os.path.dirname(dest))
        if near and not folders.writable(near):
            # In words, not the errno: the folder is what the person can fix.
            raise HandoverError(
                f"{folders.cannot_write(near)} {folders.fix_command([near])}") from e
        raise HandoverError(f"Could not copy into the Plex library: {e}") from e

    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            plex.scan(section, _plex_folder(plex, section, os.path.dirname(rel)))
            if moved_from:
                plex.scan(section, _plex_folder(plex, section, moved_from))
            item = None
            deadline = time.time() + SCAN_WAIT_SECONDS
            while item is None:
                item = plex.find_file(section, 1 if k == "movies" else 4,
                                      os.path.basename(dest),
                                      title=(rec["show_title"] or rec["title"])
                                      if k == "shows" else rec["title"])
                if item is None:
                    if time.time() >= deadline:
                        raise HandoverError(
                            "The file is in the library folder, and Plex has not "
                            "listed it yet. CouchElephant keeps its copy and asks again.")
                    time.sleep(SCAN_POLL_SECONDS)
            key = str(item.get("ratingKey") or "")
            note = None
            sent = None
            now = recorder.get(rec["id"])
            at = (now["position"] if now else 0) or 0
            # A few seconds in is not a place anybody stopped watching.
            if key and at > 60:
                try:
                    plex.set_position(key, at)
                    sent = at
                except Exception as e:
                    # The recording is safe in Plex. Only the bookmark is lost,
                    # and the row says so.
                    note = f"Plex has the recording, but did not take the resume position: {e}"
    except PlexError as e:
        raise HandoverError(f"Plex did not answer the handover: {e}") from e

    recorder._set(rec["id"], state="in_plex", plex_rating_key=key,
                  moved_at=int(time.time()), error=note,
                  # Not given when Plex refused it, so it is sent again later.
                  plex_position=sent if sent is not None else (None if note else at))
    # Only now. Plex lists the file, so ours is the spare.
    try:
        os.remove(src)
    except OSError:
        pass


def _run(rec):
    try:
        _attempt(rec)
    finally:
        # A failure for this folder turns the notice red, and a copy that
        # went through clears it.
        try:
            folders.check_recording(rec["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("folder check after the handover failed: %s", e)


def _attempt(rec):
    try:
        hand_over(rec)
    except HandoverError as e:
        recorder._set(rec["id"], state="finished", error=str(e),
                      tried_at=int(time.time()))
    except Exception as e:
        recorder._set(rec["id"], state="finished",
                      error=f"The handover to Plex failed: {type(e).__name__}: {e}",
                      tried_at=int(time.time()))


def due(now: int) -> list:
    """Finished recordings nobody is watching, oldest first.

    'moving' is in the list for a restart: a move this process is not running
    was cut off, and starts again from the copy check.
    """
    rows = db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('finished', 'moving') ORDER BY ended_at")
    return [r for r in rows
            if not recorder.watched_recently(r)
            # The viewing copy is still being made from the file we would move.
            and not recorder.busy(r["id"])
            and (r["tried_at"] or 0) <= now - RETRY_SECONDS]


def tick(now: int | None = None, wait: bool = False) -> int | None:
    """Start the next handover, if one is due and none is running.

    One at a time, in its own thread: a copy to a network disk takes minutes,
    and the recorder loop must keep starting recordings meanwhile.
    """
    global _worker
    now = now or int(time.time())
    with _lock:
        if _worker and _worker.is_alive():
            return
        rows = due(now)
        if not rows:
            return
        rec = rows[0]
        if why_unavailable(rec):
            # Not set up is not a failure to retry every ten minutes and not
            # an error to show on every recording. It is held, and says why.
            why = why_unavailable(rec)
            if rec["error"] != why:
                recorder._set(rec["id"], error=why, tried_at=now)
            return
        _worker = threading.Thread(target=_run, args=(rec,), daemon=True,
                                   name="ce-handover")
        _worker.start()
        w = _worker
    if wait:
        w.join()
    return rec["id"]


# Plex hears a new position once the player has been quiet this long, not
# every few seconds while it plays.
RESUME_QUIET_SECONDS = 60


def send_positions(now: int | None = None) -> None:
    """Tell Plex where a recording was left, when it was watched in
    CouchElephant after the handover. Without this Plex kept the position
    from the day of the move."""
    now = now or int(time.time())
    rows = [r for r in db.query(
        "SELECT * FROM recordings WHERE state = 'in_plex' "
        "AND plex_rating_key IS NOT NULL AND plex_rating_key != '' "
        "AND position > 60 AND watched_at IS NOT NULL AND watched_at <= ?",
        (now - RESUME_QUIET_SECONDS,))
        if abs((r["position"] or 0) - (r["plex_position"] or 0)) >= 5]
    if not rows:
        return
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            for r in rows:
                plex.set_position(r["plex_rating_key"], r["position"])
                recorder._set(r["id"], plex_position=r["position"])
    except (PlexError, httpx.HTTPError) as e:
        # Tried again on the next pass. Plex being away for a while is not
        # worth more than a log line.
        log.info("resume position not sent to Plex yet: %s", e)


def tidy(now: int | None = None) -> None:
    """Remove viewing copies Plex has had long enough, and rows for
    recordings that are gone from Plex too."""
    now = now or int(time.time())
    try:
        keep = max(0, int(db.get_setting("recorder_keep_days") or 2)) * 86400
    except ValueError:
        keep = 2 * 86400
    for rec in db.query("SELECT * FROM recordings WHERE state = 'in_plex' "
                        "AND moved_at <= ?", (now - keep,)):
        if recorder.watched_recently(rec) or recorder.busy(rec["id"]):
            continue
        if rec["plex_path"] and os.path.exists(rec["plex_path"]):
            shutil.rmtree(recorder.view_dir(rec["uid"]), ignore_errors=True)
            shutil.rmtree(os.path.join(recorder.rec_dir(rec["uid"]), "od"), ignore_errors=True)
        elif _library_present(rec):
            # Deleted in Plex. The library is there and the file is not in
            # it, so there is nothing left to list or to play.
            shutil.rmtree(recorder.rec_dir(rec["uid"]), ignore_errors=True)
            with db.tx() as c:
                c.execute("DELETE FROM recordings WHERE id = ?", (rec["id"],))


def _library_present(rec) -> bool:
    """Whether the folder the file would be in is there to be believed."""
    if LIB_DIR:
        return os.path.isdir(LIB_DIR)
    folder = (db.get_setting(f"recorder_{_kind(rec)}_folder") or "").strip()
    return os.path.isabs(folder) and os.path.isdir(folder)


def delete(rec) -> None:
    """Remove a recording for good: here, and from Plex if Plex has it.

    Asked for by a person, from a client allowed to. Plex is asked to delete
    its own item first, which also removes the file where Plex is set to allow
    that. The file is ours, we put it there, so if it is still on disk after
    Plex has answered it is removed here and Plex is told to look again.
    Raises HandoverError with the reason when Plex cannot be reached, and
    nothing is removed in that case.
    """
    if rec["state"] == "moving":
        raise HandoverError("This recording is being moved to Plex. Try again in a minute.")
    if rec["state"] == "in_plex":
        k = _kind(rec)
        section = (db.get_setting(f"recorder_{k}_section") or "").strip()
        try:
            with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
                if rec["plex_rating_key"]:
                    plex.delete_item(rec["plex_rating_key"])
                path = rec["plex_path"]
                if path and os.path.exists(path):
                    os.remove(path)
                    if section:
                        plex.scan(section)
        except (PlexError, httpx.HTTPError) as e:
            # Not answering at all is an httpx error and never a PlexError.
            raise HandoverError(f"Plex did not answer, so nothing was deleted: {e}") from e
        except OSError as e:
            raise HandoverError(f"Could not remove the file from the Plex library: {e}") from e
    recorder.cancel(rec["id"])
