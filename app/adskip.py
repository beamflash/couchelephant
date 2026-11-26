"""Finding the ad breaks in a recording, so a player can offer to skip them.

comskip reads the broadcast file and writes where each break starts and ends.
Two settings, both on unless turned off:

- `adskip_live`: while a recording or a live channel is still being made,
  comskip follows the growing file. A break is shown only once it ended a
  minute or more behind the point comskip has reached, because comskip still
  moves a break near that point. Tested on a real episode: the live run first
  had a break as 14:46 to 16:01, and the finished run as 12:01 to 16:01. A
  break shown too short skips less, never part of the programme.
- `adskip_final`: once a recording has ended, its breaks are worked out from
  the whole file. A live run that reaches the end of a recording writes the
  same answer, so then nothing is scanned twice.

Nothing ever skips on its own. A player shows a Skip button inside a break,
and a break found wrongly costs one press.

The times are seconds from the start of the broadcast file. The copy a player
watches is made from the same file from its first byte, so they match.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from . import db, paths, recorder

log = logging.getLogger("couchelephant.adskip")

COMSKIP = paths.tool("comskip")
# A break must have ended this long before the point a live scan has reached
# before a player is shown it.
LIVE_SETTLE_SECONDS = 60
# A failed scan of a finished recording is tried again after this long, and
# not at all after this many failures.
RETRY_SECONDS = 3600
MAX_FAILS = 3
# A live scan that stops while its recording goes on (a tuner that dropped
# for longer than comskip waits) is started again, this many times.
LIVE_RESTARTS = 5
# Less than this and comskip has nothing to look at yet.
LIVE_START_BYTES = 2 * 1024 * 1024

# The detection settings that matched Plex's own ad markers to a tenth of a
# second on two recordings here (2026-11-17).
_INI = """detect_method=43
verbose=0
max_commercialbreak=600
min_commercialbreak=25
max_commercial_size=125
min_commercial_size=4
min_show_segment_length=250
disable_heuristics=4
output_edl=1
output_default=0
output_txt=0
"""
# Live: keep reading the growing file. comskip waits four seconds for each
# retry at the end of the file, so 45 rides out three minutes of a tuner that
# dropped and came back; the recorder tries again every few seconds.
_LIVE_INI = _INI + """live_tv=1
live_tv_retries=45
"""

_PROGRESS = re.compile(r"(\d+):(\d\d):(\d\d) - \d+ frames")


def why_unavailable() -> str | None:
    if not shutil.which(COMSKIP):
        return "comskip is not installed in this container."
    return None


def available() -> bool:
    return why_unavailable() is None


def _on(key: str) -> bool:
    return (db.get_setting(key) or "1") == "1"


def live_on() -> bool:
    return _on("adskip_live")


def final_on() -> bool:
    return _on("adskip_final")


# ---------- one comskip run ----------

class Scan:
    def __init__(self, rec, src: str, live: bool):
        self.rec_id, self.live, self.src = rec["id"], live, src
        self.dir = os.path.join(recorder.rec_dir(rec["uid"]), "ads")
        # Only inside a folder that still exists: a live channel closed or a
        # recording deleted a moment ago must not have its folder made again.
        if not os.path.isdir(recorder.rec_dir(rec["uid"])):
            raise OSError(f"{recorder.rec_dir(rec['uid'])} is gone")
        os.makedirs(self.dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        self.edl = os.path.join(self.dir, stem + ".edl")
        self.log_path = os.path.join(self.dir, "comskip.log")
        try:
            os.remove(self.edl)
        except OSError:
            pass
        ini = os.path.join(self.dir, "comskip.ini")
        with open(ini, "w") as f:
            f.write(_LIVE_INI if live else _INI)
        with open(self.log_path, "wb") as out:
            # Last in line for the processor: the recording and the copy
            # people watch come first.
            self.proc = subprocess.Popen(
                ["nice", "-n", "19", COMSKIP, f"--ini={ini}", f"--output={self.dir}", src],
                stdout=out, stderr=subprocess.STDOUT, cwd=self.dir)

    def running(self) -> bool:
        return self.proc.poll() is None

    def succeeded(self) -> bool:
        """comskip on Linux exits 0 when it found ads and 1 when it found
        none (`exit(!result)` in its source). Both are a finished scan; a
        programme with no ads is an answer, not a failure."""
        code = self.proc.returncode
        if code == 0:
            return True
        if code == 1:
            try:
                with open(self.log_path, "rb") as f:
                    f.seek(max(0, os.path.getsize(self.log_path) - 4096))
                    return b"Commercials were not found" in f.read()
            except OSError:
                return False
        return False

    def breaks(self) -> list[tuple[float, float]]:
        return read_edl(self.edl)

    def reached(self) -> float:
        """How far into the file comskip has got, in seconds."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log_path) - 4096))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return 0.0
        found = _PROGRESS.findall(tail)
        if not found:
            return 0.0
        h, m, s = found[-1]
        return int(h) * 3600 + int(m) * 60 + int(s)

    def stop(self):
        if self.running():
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def read_edl(path: str) -> list[tuple[float, float]]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        start, end = float(parts[0]), float(parts[1])
                    except ValueError:
                        continue
                    if end > start:
                        out.append((round(start, 2), round(end, 2)))
    except OSError:
        pass
    return out


# ---------- the rows ----------

def _store_live(rec_id: int, breaks, edge: float) -> None:
    """Breaks from a scan still following the file. Rows this scan has not
    settled again yet (ending past `edge`) are kept: a scan started again (after a tuner dropped, or
    a restart) reads from the beginning, and the breaks shown before must not
    vanish while it catches up."""
    kept = [(r["start_s"], r["end_s"]) for r in db.query(
        "SELECT start_s, end_s FROM ad_breaks WHERE recording_id = ? AND end_s > ?",
        (rec_id, edge))]
    _store(rec_id, sorted(set(list(breaks) + kept)), final=False)


def _store(rec_id: int, breaks, final: bool) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM ad_breaks WHERE recording_id = ?", (rec_id,))
        c.executemany(
            "INSERT INTO ad_breaks (recording_id, start_s, end_s, final) VALUES (?, ?, ?, ?)",
            [(rec_id, s, e, 1 if final else 0) for s, e in breaks])


def breaks_for(rec) -> dict:
    """What a player needs: the breaks to offer a Skip button in, and whether
    more may still come."""
    growing = rec["state"] in ("scheduled", "recording")
    shown = live_on() if growing else final_on()
    rows = db.query("SELECT start_s, end_s, final FROM ad_breaks WHERE recording_id = ? "
                    "ORDER BY start_s", (rec["id"],)) if shown else []
    # A sports broadcast opens on its team intro, which comskip reads as an
    # ad because it has no programme before it (a 0 to 188 s "break" found on
    # a football game). A real break never starts on the first second of a
    # game, so one that does is not offered. Kept in the table, only unshown.
    if rec["section"] == "sports":
        rows = [r for r in rows if r["start_s"] >= 1]
    return {"ok": True,
            "breaks": [{"start": r["start_s"], "end": r["end_s"]} for r in rows],
            "final": rec["ads_final"] == 1,
            "growing": growing}


# ---------- the loop ----------

_scans: dict[int, Scan] = {}
_restarts: dict[int, int] = {}
_lock = threading.RLock()


def tick(now: int | None = None) -> None:
    """Called with the recorder's loop, every few seconds."""
    if not available():
        return
    now = now or int(time.time())
    with _lock:
        _forget_gone()
        _follow_running(now)
        if live_on():
            _start_live()
        if final_on():
            _start_final(now)


def _forget_gone():
    """A live channel that closed, or a recording deleted: its scan and rows go."""
    with db.tx() as c:
        c.execute("DELETE FROM ad_breaks WHERE recording_id NOT IN (SELECT id FROM recordings)")
    for rec_id in list(_scans):
        if not recorder.get(rec_id):
            _scans.pop(rec_id).stop()
    for rec_id in list(_restarts):
        if not recorder.get(rec_id):
            _restarts.pop(rec_id, None)


def _follow_running(now):
    for rec_id, scan in list(_scans.items()):
        rec = recorder.get(rec_id)
        if not rec:
            _scans.pop(rec_id).stop()
            continue
        growing = rec["state"] in ("scheduled", "recording")
        if scan.live and not live_on() or not scan.live and not final_on():
            # Turned off: stop now, and keep what was found.
            scan.stop()
            _scans.pop(rec_id)
            continue
        if scan.running():
            if scan.live:
                edge = scan.reached() - LIVE_SETTLE_SECONDS
                _store_live(rec_id, [(s, e) for s, e in scan.breaks() if e <= edge], edge)
            continue
        _scans.pop(rec_id)
        ok = scan.succeeded()
        if scan.live and growing:
            # comskip took a pause in the stream for the end. Again from the
            # start, which takes it a minute or two to catch up.
            _restarts[rec_id] = _restarts.get(rec_id, 0) + 1
            continue
        if ok and rec["kind"] == "recording":
            _store(rec_id, scan.breaks(), final=True)
            recorder._set(rec_id, ads_final=1, ads_tried_at=now)
            _restarts.pop(rec_id, None)
        elif not ok and not scan.live:
            fails = (rec["ads_fails"] or 0) + 1
            log.warning("comskip failed on recording %s (%s of %s)", rec_id, fails, MAX_FAILS)
            recorder._set(rec_id, ads_fails=fails, ads_tried_at=now)


def _start_live():
    for rec in db.query("SELECT * FROM recordings WHERE state = 'recording' "
                        "AND (bytes IS NULL OR bytes >= ?)", (LIVE_START_BYTES,)):
        if rec["id"] in _scans or _restarts.get(rec["id"], 0) >= LIVE_RESTARTS:
            continue
        src = recorder.broadcast_path(rec["uid"])
        if not os.path.exists(src) or os.path.getsize(src) < LIVE_START_BYTES:
            continue
        try:
            _scans[rec["id"]] = Scan(rec, src, live=True)
        except OSError as e:
            log.warning("could not start comskip on recording %s: %s", rec["id"], e)
            _restarts[rec["id"]] = LIVE_RESTARTS


def _start_final(now):
    """One finished recording at a time: the processor has better things to do."""
    if any(not s.live for s in _scans.values()):
        return
    for rec in db.query(
            "SELECT * FROM recordings WHERE kind = 'recording' "
            "AND state IN ('finished', 'moving', 'in_plex') "
            "AND COALESCE(ads_final, 0) = 0 AND COALESCE(ads_fails, 0) < ? "
            "AND COALESCE(ads_tried_at, 0) <= ? ORDER BY ended_at",
            (MAX_FAILS, now - RETRY_SECONDS)):
        if rec["id"] in _scans:
            continue
        src = recorder.broadcast_path(rec["uid"])
        if not os.path.exists(src):
            src = rec["plex_path"] or ""
        if not src or not os.path.exists(src):
            # Nothing left to read. Not a failure to try again.
            recorder._set(rec["id"], ads_final=-1)
            continue
        try:
            _scans[rec["id"]] = Scan(rec, src, live=False)
        except OSError as e:
            recorder._set(rec["id"], ads_fails=(rec["ads_fails"] or 0) + 1, ads_tried_at=now)
            log.warning("could not start comskip on recording %s: %s", rec["id"], e)
        return


def shutdown() -> None:
    with _lock:
        for scan in _scans.values():
            scan.stop()
        _scans.clear()
