"""Segments made when a player asks for them.

A recording that is in Plex has its viewing copy tidied away after a while. A
game of four hours cannot be converted whole before it can be watched, and a
player cannot seek to a place the copy does not have yet. So for such a
recording the server lists EVERY segment for the whole duration in one VOD
playlist, and makes a segment only when it is asked for, the way Plex and
Jellyfin do. One ffmpeg per recording, started at the asked-for segment and
left to run on ahead of the player. A request far from where it is makes it
start again at the request. It stops when nobody has asked for a while.

The segments are kept in rec_dir/od, apart from the viewing copy.
"""
import logging
import os
import subprocess
import threading
import time

from . import db, recorder

log = logging.getLogger("couchelephant.ondemand")

SEG = recorder.SEGMENT_SECONDS
# How long a request waits for its segment.
WAIT_SECONDS = 20
# A running ffmpeg is waited for when the request is this many segments ahead
# of what it has made; further than that it is started again at the request.
AHEAD = 8
# Stopped when nobody asked for this long, or when it is this many segments
# past the last request (a player buffers about a minute).
IDLE_SECONDS = 60
RUNAWAY = 40


def od_dir(uid: str) -> str:
    return os.path.join(recorder.rec_dir(uid), "od")


def _source(rec) -> str | None:
    for p in (rec["plex_path"], recorder.broadcast_path(rec["uid"])):
        if p and os.path.exists(p):
            return p
    return None


def use(uid: str):
    """The recording row when its stream is made on demand: in Plex, no whole
    viewing copy, nothing of ours converting it, and a file to read from."""
    rec = db.one("SELECT * FROM recordings WHERE uid = ?", (uid,))
    if (not rec or rec["state"] != "in_plex" or recorder.busy(rec["id"])
            or recorder.view_info(uid)["complete"] or not _source(rec)):
        return None
    return rec


def cached_duration(uid: str) -> float | None:
    try:
        with open(os.path.join(od_dir(uid), "duration")) as f:
            return float(f.read())
    except (OSError, ValueError):
        return None


def duration(rec) -> float | None:
    d = cached_duration(rec["uid"])
    if d:
        return d
    try:
        r = subprocess.run([recorder.FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=nw=1:nk=1", _source(rec)],
                           capture_output=True, text=True, timeout=60)
        d = float(r.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    os.makedirs(od_dir(rec["uid"]), exist_ok=True)
    with open(os.path.join(od_dir(rec["uid"]), "duration"), "w") as f:
        f.write(str(d))
    return d


def playlist(rec, start0: bool = False) -> str | None:
    """A VOD playlist of every segment, so a player can go anywhere at once."""
    d = duration(rec)
    if not d:
        return None
    count = int(-(-d // SEG))
    out = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{SEG + 1}",
           "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
           "#EXT-X-INDEPENDENT-SEGMENTS"]
    if start0:
        out.append("#EXT-X-START:TIME-OFFSET=0,PRECISE=YES")
    for i in range(count):
        length = min(SEG, d - i * SEG)
        out += [f"#EXTINF:{length:.6f},", f"seg{i:05d}.ts"]
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


class _Run:
    def __init__(self, proc, start, enc, log_file):
        self.proc, self.start, self.enc = proc, start, enc
        self.spawned = time.time()
        self.last_ask = self.spawned
        self.want = start
        self.log_file = log_file


_runs: dict[str, _Run] = {}
_lock = threading.RLock()
_watcher: threading.Thread | None = None


def _progress(uid: str, run: _Run) -> int:
    """The highest segment this run has finished."""
    best = run.start - 1
    try:
        for name in os.listdir(od_dir(uid)):
            if name.startswith("seg") and name.endswith(".ts") and len(name) == 11:
                n = int(name[3:8])
                if n > best and os.path.getmtime(os.path.join(od_dir(uid), name)) >= run.spawned:
                    best = n
    except (OSError, ValueError):
        pass
    return best


def _kill(uid: str):
    run = _runs.pop(uid, None)
    if run and run.proc.poll() is None:
        run.proc.kill()
        try:
            run.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _spawn(rec, n: int, enc: str | None = None) -> _Run:
    uid = rec["uid"]
    os.makedirs(od_dir(uid), exist_ok=True)
    src = _source(rec)
    enc = enc or recorder.encoder()
    laced = recorder.interlaced(src) if enc == "vaapi" else False
    logf = open(os.path.join(od_dir(uid), "ffmpeg.log"), "wb")
    proc = subprocess.Popen(
        recorder.convert_args(enc, od_dir(uid), laced, src=src, start_seg=n),
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
    logf.close()
    run = _Run(proc, n, enc, None)
    _runs[uid] = run
    _start_watcher()
    return run


def segment(rec, name: str, wait: float | None = None) -> str | None:
    """The path of a segment, made now if it is not there. None when there is
    no such segment or it could not be made in time."""
    uid = rec["uid"]
    path = os.path.join(od_dir(uid), name)
    n = int(name[3:8])
    with _lock:
        run = _runs.get(uid)
        if run:
            run.last_ask = time.time()
            run.want = n
    if os.path.exists(path):
        return path
    d = duration(rec)
    if not d or n >= int(-(-d // SEG)):
        return None
    deadline = time.time() + (WAIT_SECONDS if wait is None else wait)
    with _lock:
        run = _runs.get(uid)
        near = (run and run.proc.poll() is None and run.start <= n
                and n <= _progress(uid, run) + AHEAD)
        if not near:
            _kill(uid)
            run = _spawn(rec, n)
        run.last_ask = time.time()
        run.want = n
    while time.time() < deadline:
        if os.path.exists(path):
            return path
        with _lock:
            run = _runs.get(uid)
            if run is None:
                # Stopped by the watcher while we waited (it had run far
                # ahead of the last request): start again here.
                run = _spawn(rec, n)
            if run.proc.poll() is not None and not os.path.exists(path):
                if run.proc.returncode != 0 and run.enc == "vaapi":
                    # The graphics chip failed on a real file: software from
                    # now on, as the whole-copy path does.
                    log.warning("hardware encoding failed for %s, using software", uid)
                    recorder._vaapi = False
                    _runs.pop(uid, None)
                    run = _spawn(rec, n, "software")
                else:
                    return None
        time.sleep(0.05)
    return path if os.path.exists(path) else None


def _watch():
    while True:
        time.sleep(2)
        with _lock:
            now = time.time()
            for uid, run in list(_runs.items()):
                if run.proc.poll() is not None:
                    _runs.pop(uid, None)
                elif (now - run.last_ask > IDLE_SECONDS
                      or _progress(uid, run) - run.want > RUNAWAY):
                    _kill(uid)


def _start_watcher():
    global _watcher
    if _watcher is None or not _watcher.is_alive():
        _watcher = threading.Thread(target=_watch, daemon=True)
        _watcher.start()


def shutdown():
    with _lock:
        for uid in list(_runs):
            _kill(uid)
