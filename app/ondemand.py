"""Segments made when a player asks for them.

A recording that is in Plex has its viewing copy tidied away after a while. A
game of four hours cannot be converted whole before it can be watched, and a
player cannot seek to a place the copy does not have yet. So for such a
recording the server lists EVERY segment for the whole duration in one VOD
playlist, and makes a segment only when it is asked for, the way Plex and
Jellyfin do.

Each ffmpeg ("run") starts at the asked-for segment and goes on ahead of the
player. Several people can watch one recording at different places, so a
recording may have a few runs (MAX_RUNS), each covering the place its viewer
is at. A request is served by the run that covers it, or starts a new one; the
least recently asked run is stopped when there are too many. A run that gets
far ahead of its viewer is paused (SIGSTOP), not killed, and goes on when the
viewer catches up, so the only places two runs meet are seeks. It is stopped
when nobody has asked for a while.

Every run puts segment N at the same time stamps (-ss with
-output_ts_offset), so segments made by different runs join without a jump.
The segments are kept in rec_dir/od, apart from the viewing copy.
"""
import ctypes
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

from . import db, recorder

log = logging.getLogger("couchelephant.ondemand")

SEG = recorder.SEGMENT_SECONDS
# How long a request waits for its segment.
WAIT_SECONDS = 20
# A running ffmpeg covers a request this many segments ahead of what it has
# made; further than that a new one is started at the request.
AHEAD = 8
# Several viewers of one recording, each with an ffmpeg.
MAX_RUNS = 3
# A run is paused this far (in segments) ahead of its viewer, and goes on when
# the viewer is this close (a player buffers about a minute).
PAUSE_AT = 15
RESUME_AT = 8
# Stopped when nobody asked for this long.
IDLE_SECONDS = 60
# A source that cannot be made into segments is not tried again for this long.
FAIL_SECONDS = 30
# A request whose run was stopped under it starts again this many times.
RESTARTS = 2
LOG_CAP = 200_000

_SEGMENT = re.compile(r"seg[0-9]{5}\.ts")
_CHIP = re.compile(r"vaapi|/dev/dri|hwupload|libva|hardware|device creation", re.I)


def od_dir(uid: str) -> str:
    return os.path.join(recorder.rec_dir(uid), "od")


def _source(rec) -> str | None:
    for p in (rec["plex_path"], recorder.broadcast_path(rec["uid"])):
        if p and os.path.exists(p):
            return p
    return None


def use(uid: str):
    """The recording row when its stream is made on demand: in Plex, nothing of
    ours converting it, no whole viewing copy, and a file to read from."""
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
    src = _source(rec)
    if not src:
        return None
    try:
        r = subprocess.run([recorder.FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=nw=1:nk=1", src],
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


# ---------- runs ----------

class _Run:
    def __init__(self, uid, proc, start, enc, slot, log_path, log_from):
        self.uid, self.proc, self.start, self.enc = uid, proc, start, enc
        self.slot, self.log_path, self.log_from = slot, log_path, log_from
        self.spawned = time.time()
        self.last_ask = self.spawned
        self.want = start
        self.done = start - 1          # the highest segment this run has made
        self.paused = False
        self.dead = False              # stopped by us
        self.handled = False           # its exit has been dealt with
        self.outcome = ""

    def segment_path(self, n: int) -> str:
        return os.path.join(od_dir(self.uid), f"seg{n:05d}.ts")

    def progress(self) -> int:
        """Counted from this run's own files only: a segment an earlier run
        left behind is older than the run."""
        while True:
            try:
                if os.path.getmtime(self.segment_path(self.done + 1)) < self.spawned:
                    return self.done
            except OSError:
                return self.done
            self.done += 1

    def covers(self, n: int) -> bool:
        return (not self.dead and self.proc.poll() is None
                and self.start <= n <= self.progress() + AHEAD)

    def words(self) -> str:
        """What this run's ffmpeg said, last three lines."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self.log_from)
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return "no log"
        return " | ".join(text.strip().splitlines()[-3:])


_runs: dict[str, list[_Run]] = {}
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_failed: dict[str, tuple[float, str]] = {}
_laced: dict[str, bool] = {}
_down = False
_watcher: threading.Thread | None = None


def _lock(uid: str) -> threading.RLock:
    with _guard:
        return _locks.setdefault(uid, threading.RLock())


def running(uid: str) -> bool:
    return any(r.proc.poll() is None for r in _runs.get(uid, []))


def allow():
    """For tests: spawning is allowed again after shutdown."""
    global _down
    _down = False
    _failed.clear()


def _pdeath():
    # Dies with the process that started it, so a crash leaves no ffmpeg.
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


# The parent-death signal fires when the THREAD that started the child ends,
# and request threads come and go, so every ffmpeg is started from one thread
# that stays.
_jobs: queue.Queue = queue.Queue()
_starter: threading.Thread | None = None


def _start_loop():
    while True:
        args, kwargs, box, done = _jobs.get()
        try:
            box.append(subprocess.Popen(args, **kwargs))
        except OSError as e:
            box.append(e)
        done.set()


def _popen(args, **kwargs) -> subprocess.Popen:
    global _starter
    with _guard:
        if _starter is None or not _starter.is_alive():
            _starter = threading.Thread(target=_start_loop, daemon=True)
            _starter.start()
    if sys.platform.startswith("linux"):
        kwargs["preexec_fn"] = _pdeath
    box: list = []
    done = threading.Event()
    _jobs.put((args, kwargs, box, done))
    done.wait()
    if isinstance(box[0], Exception):
        raise box[0]
    return box[0]


def _kill(run: _Run):
    """Stops one run. Never called with a recording's lock held."""
    run.dead = True
    if run.proc.poll() is None:
        run.proc.kill()
        try:
            run.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _laced_of(src: str) -> bool:
    if src not in _laced:
        _laced[src] = recorder.interlaced(src)
    return _laced[src]


def _open_log(uid: str, slot: int):
    path = os.path.join(od_dir(uid), f"ffmpeg-{slot}.log")
    try:
        if os.path.getsize(path) > LOG_CAP:
            os.replace(path, path + ".1")
    except OSError:
        pass
    return path


def _spawn(rec, n: int, enc: str, laced: bool, src: str) -> _Run:
    """Called with the recording's lock held. A new ffmpeg at segment n."""
    uid = rec["uid"]
    os.makedirs(od_dir(uid), exist_ok=True)
    used = {r.slot for r in _runs.get(uid, []) if not r.dead}
    slot = next(i for i in range(MAX_RUNS + 2) if i not in used)
    path = _open_log(uid, slot)
    with open(path, "ab") as logf:
        logf.write(f"--- start at segment {n} ---\n".encode())
        logf.flush()
        log_from = logf.tell()
        proc = _popen(recorder.convert_args(enc, od_dir(uid), laced, src=src, start_seg=n),
                      stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
    run = _Run(uid, proc, n, enc, slot, path, log_from)
    _runs.setdefault(uid, []).append(run)
    _start_watcher()
    return run


def _same_encoder(uid: str, enc: str) -> list[_Run]:
    """One encoder per recording's od/: when it changed (the graphics chip
    failed and software took over) what was made by the other is cleared.
    Returns the runs to stop; called with the lock held."""
    mark = os.path.join(od_dir(uid), "encoder")
    try:
        with open(mark) as f:
            was = f.read().strip()
    except OSError:
        was = ""
    victims: list[_Run] = []
    if was and was != enc:
        victims = _runs.pop(uid, [])
        for r in victims:
            r.dead = True
        try:
            for name in os.listdir(od_dir(uid)):
                if _SEGMENT.fullmatch(name):
                    os.remove(os.path.join(od_dir(uid), name))
        except OSError:
            pass
    if was != enc:
        os.makedirs(od_dir(uid), exist_ok=True)
        with open(mark, "w") as f:
            f.write(enc)
    return victims


def _cover(uid: str, n: int) -> _Run | None:
    """The run that is closest behind n and will reach it."""
    best = None
    for r in _runs.get(uid, []):
        if r.covers(n) and (best is None or r.start > best.start):
            best = r
    return best


def _resume(run: _Run):
    if run.paused and run.proc.poll() is None:
        try:
            os.kill(run.proc.pid, signal.SIGCONT)
        except OSError:
            pass
    run.paused = False


def _touch(run: _Run, n: int):
    run.last_ask = time.time()
    run.want = n
    if run.paused and run.progress() - n <= RESUME_AT:
        _resume(run)


class Ticket:
    """One request for one segment."""

    def __init__(self, rec, name: str, wait: float | None):
        self.rec, self.name = rec, name
        self.uid = rec["uid"]
        self.n = int(name[3:8])
        self.path = os.path.join(od_dir(self.uid), name)
        self.deadline = time.time() + (WAIT_SECONDS if wait is None else wait)
        self.run: _Run | None = None
        self.restarts = 0
        self.src = ""
        self.enc = ""
        self.laced = False


def _fail(uid: str, why: str):
    log.warning("no segments for %s: %s", uid, why)
    _failed[uid] = (time.time() + FAIL_SECONDS, why)


def _assign(t: Ticket) -> bool:
    """Gives the ticket the run that covers it, starting one if there is none.
    False when nothing can be started."""
    victims: list[_Run] = []
    t.run = None
    with _lock(t.uid):
        run = _cover(t.uid, t.n)
        if run is None:
            if _down:
                return False
            victims += _same_encoder(t.uid, t.enc)
            live = [r for r in _runs.get(t.uid, []) if not r.dead]
            while len(live) >= MAX_RUNS:
                old = min(live, key=lambda r: r.last_ask)
                live.remove(old)
                old.dead = True
                _runs[t.uid].remove(old)
                victims.append(old)
            try:
                run = _spawn(t.rec, t.n, t.enc, t.laced, t.src)
            except OSError as e:
                _fail(t.uid, f"ffmpeg did not start: {e}")
                run = None
        if run is not None:
            _touch(run, t.n)
            t.run = run
    for v in victims:
        _kill(v)
    return t.run is not None


def begin(rec, name: str, wait: float | None = None) -> Ticket | None:
    """Starts getting a segment. May run probes, so not on the event loop."""
    t = Ticket(rec, name, wait)
    if os.path.exists(t.path):
        run = _cover(t.uid, t.n)
        if run:
            _touch(run, t.n)
        return t
    if _down:
        return None
    gone = _failed.get(t.uid)
    if gone and time.time() < gone[0]:
        return None
    d = duration(rec)
    if not d or t.n >= int(-(-d // SEG)):
        return None
    src = _source(rec)
    if not src:
        _fail(t.uid, "the recording's file is not there")
        return None
    t.src = src
    t.enc = recorder.encoder()
    t.laced = _laced_of(src) if t.enc == "vaapi" else False
    return t if _assign(t) else None


READY, WAIT, FAIL = "ready", "wait", "fail"


def _handle_exit(run: _Run) -> str:
    """A run's ffmpeg ended on its own. Deals with it once and says how:
    "ok" (reached the end), "chip" (the graphics chip failed), "killed" (by a
    signal, someone else's doing) or "failed"."""
    if run.handled:
        return run.outcome
    run.handled = True
    rc = run.proc.returncode
    if rc == 0:
        run.outcome = "ok"
    elif rc < 0:
        run.outcome = "killed"
        log.warning("ffmpeg for %s was stopped by signal %d", run.uid, -rc)
    else:
        said = run.words()
        if run.enc == "vaapi" and _CHIP.search(said):
            # The graphics chip failed on a real file: software from now on,
            # as the whole-copy path does.
            log.warning("hardware encoding failed for %s, using software: %s", run.uid, said)
            recorder._vaapi = False
            run.outcome = "chip"
        else:
            log.warning("ffmpeg failed for %s at segment %d: %s", run.uid, run.start, said)
            _fail(run.uid, said)
            run.outcome = "failed"
    return run.outcome


def poll(t: Ticket) -> str:
    """Where a request stands. Never blocks."""
    if os.path.exists(t.path):
        return READY
    if time.time() >= t.deadline:
        return FAIL
    run = t.run
    if run is None:
        return FAIL
    if run.dead or run.proc.poll() is not None:
        if not run.dead and _handle_exit(run) in ("ok", "failed"):
            return FAIL
        if t.restarts >= RESTARTS or _down:
            return FAIL
        # Stopped under us (another viewer's seek, a chip failure): start
        # again, here.
        t.restarts += 1
        t.enc = recorder.encoder()
        t.laced = _laced_of(t.src) if t.enc == "vaapi" else False
        return WAIT if _assign(t) else FAIL
    with _lock(t.uid):
        _touch(run, t.n)
    return WAIT


def segment(rec, name: str, wait: float | None = None) -> str | None:
    """The path of a segment, made now if it is not there (blocking form)."""
    t = begin(rec, name, wait)
    if t is None:
        return None
    while True:
        how = poll(t)
        if how == READY:
            return t.path
        if how == FAIL:
            return t.path if os.path.exists(t.path) else None
        time.sleep(0.05)


def begin_request(uid: str, name: str) -> Ticket | None:
    """For a request: the recording row and the first step in one go."""
    rec = use(uid)
    return begin(rec, name) if rec else None


# ---------- the watcher ----------

def _watch():
    while True:
        time.sleep(0.5)
        now = time.time()
        victims: list[_Run] = []
        for uid in list(_runs):
            with _lock(uid):
                keep = []
                for r in _runs.get(uid, []):
                    if r.dead:
                        continue
                    if r.proc.poll() is not None:
                        _handle_exit(r)
                        continue
                    if now - r.last_ask > IDLE_SECONDS:
                        r.dead = True
                        victims.append(r)
                        continue
                    if not r.paused and r.progress() - r.want > PAUSE_AT:
                        try:
                            os.kill(r.proc.pid, signal.SIGSTOP)
                            r.paused = True
                        except OSError:
                            pass
                    keep.append(r)
                if keep:
                    _runs[uid] = keep
                else:
                    _runs.pop(uid, None)
        for v in victims:
            _kill(v)


def _start_watcher():
    global _watcher
    with _guard:
        if _watcher is None or not _watcher.is_alive():
            _watcher = threading.Thread(target=_watch, daemon=True)
            _watcher.start()


def shutdown():
    global _down
    _down = True
    victims = []
    for uid in list(_runs):
        with _lock(uid):
            victims += _runs.pop(uid, [])
    for v in victims:
        v.dead = True
        _kill(v)
