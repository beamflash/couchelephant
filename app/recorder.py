"""CouchElephant's own recorder: the tuner to disk, and a copy you can watch.

Two files for every recording, made by two separate workers on purpose.

The BROADCAST file is the stream exactly as the tuner sent it. Nothing decodes
it, so nothing can fall behind and nothing can corrupt it. This is the file
Plex is given.

The VIEWING copy is an HLS event playlist made from the broadcast file by
ffmpeg. An event playlist only grows and never drops a segment, which is what
lets a player start at the beginning of a recording that is still being made.
It is disposable: if ffmpeg dies, the broadcast file is whole and the copy is
made again from the first byte.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid

import httpx

from . import db, tuner

log = logging.getLogger("couchelephant.recorder")

REC_DIR = os.environ.get("COUCHELEPHANT_RECORDINGS", "/recordings")
FFMPEG = os.environ.get("COUCHELEPHANT_FFMPEG", "ffmpeg")
VAAPI_DEVICE = "/dev/dri/renderD128"
SEGMENT_SECONDS = 4
# A live session nobody has kept alive for this long is closed, and its tuner
# given back.
LIVE_IDLE_SECONDS = 60
# The longest a live session may hold a tuner without a recording behind it.
LIVE_MAX_SECONDS = 12 * 3600
# How many times the viewing copy is started again for one recording.
CONVERT_RETRIES = 3

ACTIVE = ("scheduled", "recording")


# ---------- is the feature here at all ----------

def why_unavailable() -> str | None:
    """None when the recorder can run, or a sentence saying what is missing."""
    if not os.path.isdir(REC_DIR):
        return (f"There is no {REC_DIR} folder. Mount one on the container to "
                "let CouchElephant record.")
    if not os.access(REC_DIR, os.W_OK):
        return f"CouchElephant cannot write to {REC_DIR}."
    if not shutil.which(FFMPEG):
        return "ffmpeg is not installed in this container."
    return None


def available() -> bool:
    return why_unavailable() is None


# ---------- where things live ----------

def rec_dir(uid: str) -> str:
    return os.path.join(REC_DIR, uid)


def broadcast_path(uid: str) -> str:
    return os.path.join(rec_dir(uid), "broadcast.ts")


def view_dir(uid: str) -> str:
    return os.path.join(rec_dir(uid), "view")


def playlist_path(uid: str) -> str:
    return os.path.join(view_dir(uid), "index.m3u8")


def view_info(uid: str) -> dict:
    """What the viewing copy holds right now: seconds, and whether it is closed."""
    seconds, complete, segments = 0.0, False, 0
    try:
        with open(playlist_path(uid), encoding="utf-8") as f:
            for line in f:
                if line.startswith("#EXTINF:"):
                    try:
                        seconds += float(line[8:].split(",")[0])
                        segments += 1
                    except ValueError:
                        pass
                elif line.startswith("#EXT-X-ENDLIST"):
                    complete = True
    except OSError:
        pass
    return {"seconds": seconds, "complete": complete, "segments": segments}


# ---------- the encoder ----------

_vaapi = None

# What the hardware encoder is asked for. Constant quality, not a bitrate: the
# free Intel driver that Debian ships offers no other mode, and answers
# "Invalid argument" to a bitrate target. Found on a live install, where the
# probe below passed and every real conversion failed, because the probe did
# not ask for what the conversion asked for. Both now use these same lines.
VAAPI_FILTER = "format=nv12,hwupload,deinterlace_vaapi=rate=field:auto=1"
VAAPI_ENCODE = ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "24"]


def _vaapi_works() -> bool:
    """One second of test picture through the hardware encoder. Asked once."""
    global _vaapi
    if _vaapi is None:
        _vaapi = False
        if os.path.exists(VAAPI_DEVICE):
            try:
                r = subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error",
                     "-vaapi_device", VAAPI_DEVICE,
                     "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
                     "-vf", VAAPI_FILTER, *VAAPI_ENCODE, "-f", "null", "-"],
                    capture_output=True, timeout=30)
                _vaapi = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _vaapi = False
    return _vaapi


def encoder() -> str:
    """'vaapi' or 'software', from the setting and what this machine can do."""
    want = (db.get_setting("recorder_encoder") or "auto").lower()
    if want == "software":
        return "software"
    return "vaapi" if _vaapi_works() else "software"


def convert_args(enc: str, out_dir: str) -> list[str]:
    """The ffmpeg command that turns a broadcast on stdin into the viewing copy.

    A keyframe is forced every segment length, by time and not by frame count,
    so a 30 and a 60 frame channel both cut into segments of the same length.
    """
    keyframes = f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if enc == "vaapi":
        args += ["-vaapi_device", VAAPI_DEVICE]
    args += ["-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0"]
    if enc == "vaapi":
        # Decoded on the CPU, which MPEG-2 barely notices, and encoded on the
        # GPU. Field rate, so 1080i sport keeps all 60 pictures a second.
        args += ["-vf", VAAPI_FILTER, *VAAPI_ENCODE]
    else:
        # Frame rate, not field rate: 1080p60 in software is more than a small
        # machine can make as fast as it arrives.
        args += ["-vf", "bwdif=mode=send_frame:deint=interlaced",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                 "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p"]
    args += ["-force_key_frames", keyframes,
             "-c:a", "aac", "-ac", "2", "-b:a", "160k",
             "-f", "hls", "-hls_time", str(SEGMENT_SECONDS),
             "-hls_playlist_type", "event",
             "-hls_flags", "independent_segments+program_date_time",
             "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
             os.path.join(out_dir, "index.m3u8")]
    return args


# ---------- the two workers ----------

class Capture(threading.Thread):
    """The tuner's stream, appended to a file, untouched."""

    def __init__(self, url: str, path: str):
        super().__init__(daemon=True)
        self.url, self.path = url, path
        self.bytes = 0
        self.error: str | None = None
        self.connected_at: int | None = None
        self._halt = threading.Event()
        self._resp = None

    def run(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        while not self._halt.is_set():
            try:
                with httpx.stream("GET", self.url,
                                  timeout=httpx.Timeout(10, read=15)) as r:
                    self._resp = r
                    if r.status_code == 503:
                        self.error = "Every tuner is in use."
                    else:
                        r.raise_for_status()
                        # Unbuffered, so the converter reading this file sees
                        # each chunk as soon as it is written.
                        with open(self.path, "ab", buffering=0) as f:
                            for chunk in r.iter_bytes(64 * 1024):
                                if self._halt.is_set():
                                    break
                                f.write(chunk)
                                self.bytes += len(chunk)
                                if not self.connected_at:
                                    self.connected_at = int(time.time())
                                    self.error = None
            except Exception as e:
                if not self._halt.is_set():
                    self.error = f"The tuner stream stopped: {e}"
            finally:
                self._resp = None
            # A dropped stream is tried again for as long as the recording is
            # wanted. A gap in a game is better than half a game.
            self._halt.wait(3)

    def stop(self):
        self._halt.set()
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


class Converter:
    """ffmpeg, fed the broadcast file as it grows, writing the viewing copy."""

    def __init__(self, src: str, out_dir: str, growing, enc: str | None = None):
        self.src, self.out_dir, self.growing = src, out_dir, growing
        self.enc = enc or encoder()
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self._stop = False
        self._thread = threading.Thread(target=self._feed, daemon=True)

    def start(self):
        # Always from the first byte into an empty folder. A playlist that is
        # continued from where a dead ffmpeg left it has to be exactly right
        # or the player stalls, and starting over cannot be wrong.
        shutil.rmtree(self.out_dir, ignore_errors=True)
        os.makedirs(self.out_dir, exist_ok=True)
        log = open(os.path.join(self.out_dir, "ffmpeg.log"), "wb")
        self.proc = subprocess.Popen(convert_args(self.enc, self.out_dir),
                                     stdin=subprocess.PIPE, stdout=log, stderr=log)
        log.close()
        self._thread.start()

    def _feed(self):
        proc = self.proc
        try:
            while not os.path.exists(self.src):
                if self._stop or not self.growing():
                    return
                time.sleep(0.2)
            with open(self.src, "rb") as f:
                while not self._stop:
                    chunk = f.read(256 * 1024)
                    if chunk:
                        proc.stdin.write(chunk)
                    elif not self.growing():
                        # Asked before the last read could have missed a write,
                        # so read once more and then stop.
                        chunk = f.read()
                        if chunk:
                            proc.stdin.write(chunk)
                        break
                    else:
                        time.sleep(0.2)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                self.returncode = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.returncode = proc.wait()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def failed(self) -> bool:
        return not self.alive() and self.returncode not in (0, None)

    def last_words(self) -> str:
        """The end of ffmpeg's log, for whoever has to work out why it died."""
        try:
            with open(os.path.join(self.out_dir, "ffmpeg.log"), encoding="utf-8",
                      errors="replace") as f:
                return " | ".join(f.read().strip().splitlines()[-3:])
        except OSError:
            return "no log"

    def stop(self):
        self._stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class Job:
    """One recording in flight: its capture and its converter."""

    def __init__(self, rec):
        self.id, self.uid = rec["id"], rec["uid"]
        self.capture: Capture | None = None
        self.converter: Converter | None = None
        self.convert_starts = 0
        self.capturing = False

    def start_capture(self, url: str):
        self.capture = Capture(url, broadcast_path(self.uid))
        self.capturing = True
        self.capture.start()

    def start_converter(self, src: str | None = None):
        global _vaapi
        was = self.converter
        if was and was.failed() and was.enc == "vaapi":
            # The graphics chip passed its test and then failed on a real
            # broadcast. Said in the log, with ffmpeg's own words, and the
            # processor does the work from here on: for this recording and for
            # every one after it, until the container restarts.
            log.warning("hardware encoding failed for recording %s, using software "
                        "from now on: %s", self.id, was.last_words())
            _vaapi = False
        self.convert_starts += 1
        self.converter = Converter(src or broadcast_path(self.uid),
                                   view_dir(self.uid), lambda: self.capturing)
        self.converter.start()

    def stop_capture(self):
        if self.capture:
            self.capture.stop()
            self.capture.join(timeout=20)
        # Only now may the converter treat end of file as the end.
        self.capturing = False

    def stop(self):
        self.stop_capture()
        if self.converter:
            self.converter.stop()


_jobs: dict[int, Job] = {}
_lock = threading.RLock()


# ---------- rows ----------

def get(rec_id: int):
    return db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))


def _set(rec_id: int, **cols):
    cols["updated_at"] = int(time.time())
    names = ", ".join(f"{k} = ?" for k in cols)
    with db.tx() as c:
        c.execute(f"UPDATE recordings SET {names} WHERE id = ?",
                  (*cols.values(), rec_id))


def pad_seconds(which: str) -> int:
    """Minutes of padding before ('lead') or after ('trail'), as seconds."""
    default = {"lead": "1", "trail": "3"}[which]
    try:
        return max(0, int(db.get_setting(f"recorder_{which}_minutes") or default)) * 60
    except ValueError:
        return int(default) * 60


def booked(airing=None, program_guid: str | None = None):
    """The recording of ours that covers this broadcast, or None.

    Asked by slot (channel and start) for an airing, or by programme. A failed
    row does not count: it covers nothing.
    """
    if airing is not None:
        return db.one(
            "SELECT * FROM recordings WHERE kind = 'recording' AND state != 'failed' "
            "AND (airing_id = ? OR (channel_vcn = ? AND begins_at = ?)) LIMIT 1",
            (airing["id"], airing["channel_vcn"], airing["begins_at"]))
    return db.one(
        "SELECT * FROM recordings WHERE kind = 'recording' AND state != 'failed' "
        "AND program_guid = ? LIMIT 1", (program_guid,))


def book(airing, source: str = "manual", pass_id: int | None = None,
         pass_uid: str | None = None, trail_minutes: int | None = None,
         lead_minutes: int | None = None) -> int:
    """Schedule this broadcast with our own recorder. Answers the row id.

    `airing` is a row of `airings` joined to its programme. Booking the same
    broadcast twice answers the row that already exists.
    """
    now = int(time.time())
    keys = airing.keys()
    show = airing["grandparent_title"] if "grandparent_title" in keys else None
    existing = booked(airing)
    if existing:
        # The same slot under a new name is the guide naming a game it listed
        # as a bare "NFL Football". Keep the one recording and take the name.
        if existing["airing_id"] != airing["id"] and existing["state"] in ACTIVE:
            _set(existing["id"], airing_id=airing["id"],
                 program_guid=airing["program_guid"], title=airing["title"],
                 show_title=show)
        return existing["id"]
    trail = pad_seconds("trail") if trail_minutes is None else trail_minutes * 60
    lead = pad_seconds("lead") if lead_minutes is None else lead_minutes * 60
    prog = db.one("SELECT type, season, episode, year, section FROM programs WHERE guid = ?",
                  (airing["program_guid"],))
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO recordings
               (uid, kind, state, airing_id, program_guid, title, show_title,
                ptype, season, episode, year, section,
                channel_vcn, channel_title, begins_at, ends_at, starts_at,
                stops_at, source, pass_id, pass_uid, created_at, updated_at)
               VALUES (?, 'recording', 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, airing["id"], airing["program_guid"],
             airing["title"], show,
             prog["type"] if prog else None, prog["season"] if prog else None,
             prog["episode"] if prog else None, prog["year"] if prog else None,
             prog["section"] if prog else None,
             airing["channel_vcn"],
             airing["channel_title"], airing["begins_at"], airing["ends_at"],
             airing["begins_at"] - lead,
             airing["ends_at"] + trail, source, pass_id, pass_uid, now, now))
        return cur.lastrowid


def cancel(rec_id: int) -> None:
    """Stop it if it is running, and remove the row and everything it wrote."""
    rec = get(rec_id)
    if not rec:
        return
    with _lock:
        job = _jobs.pop(rec_id, None)
    if job:
        job.stop()
    shutil.rmtree(rec_dir(rec["uid"]), ignore_errors=True)
    with db.tx() as c:
        c.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))


# ---------- live viewing ----------

def recording_on(vcn: str):
    """The recording being made on this channel right now, if there is one."""
    return db.one(
        "SELECT * FROM recordings WHERE channel_vcn = ? AND state = 'recording' "
        "ORDER BY kind = 'recording' DESC LIMIT 1", (str(vcn),))


def start_live(vcn: str, title: str | None = None, channel_title: str | None = None) -> int:
    """Watch a channel now. Joins whatever already holds it open."""
    rec = recording_on(vcn) or db.one(
        "SELECT * FROM recordings WHERE channel_vcn = ? AND kind = 'live' "
        "AND state = 'scheduled'", (str(vcn),))
    now = int(time.time())
    if rec:
        _set(rec["id"], watched_at=now)
        return rec["id"]
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO recordings
               (uid, kind, state, title, channel_vcn, channel_title, begins_at,
                ends_at, starts_at, stops_at, source, watched_at, created_at,
                updated_at)
               VALUES (?, 'live', 'scheduled', ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?)""",
            (uuid.uuid4().hex, title, str(vcn), channel_title, now,
             now + LIVE_MAX_SECONDS, now, now + LIVE_MAX_SECONDS, now, now, now))
        rec_id = cur.lastrowid
    tick()
    return rec_id


def watching(rec_id: int, position: float | None = None) -> None:
    """A player says it is on this recording, and where."""
    cols = {"watched_at": int(time.time())}
    if position is not None:
        cols["position"] = max(0.0, float(position))
    _set(rec_id, **cols)


def watched_recently(rec, within: int = 180) -> bool:
    return bool(rec["watched_at"]) and rec["watched_at"] > int(time.time()) - within


# ---------- the loop ----------

def tick(now: int | None = None) -> None:
    """One pass: start what is due, stop what is over, tidy what broke.

    Called every few seconds. It is safe to call at any time, from anywhere.
    """
    if not available():
        return
    now = now or int(time.time())
    with _lock:
        _follow_guide(now)
        _close_idle_live(now)
        _stop_due(now)
        _watch_running(now)
        _start_due(now)
        _give_up_on_missed(now)


def _follow_guide(now):
    """Move a recording that has not started when the guide moves its broadcast.

    An airing's id is made from the programme, the channel and the start, so a
    re-timed broadcast is a new airing and the old id is gone. The padding the
    row was booked with is kept.
    """
    for rec in db.query(
            "SELECT r.* FROM recordings r LEFT JOIN airings a ON a.id = r.airing_id "
            "WHERE r.kind = 'recording' AND r.state = 'scheduled' "
            "AND r.airing_id IS NOT NULL AND a.id IS NULL AND r.starts_at > ?", (now,)):
        moved = db.one(
            "SELECT * FROM airings WHERE program_guid = ? AND channel_vcn = ? "
            "AND ends_at > ? ORDER BY ABS(begins_at - ?) LIMIT 1",
            (rec["program_guid"], rec["channel_vcn"], now, rec["begins_at"]))
        if not moved or abs(moved["begins_at"] - rec["begins_at"]) > 6 * 3600:
            continue
        lead = rec["begins_at"] - rec["starts_at"]
        trail = rec["stops_at"] - rec["ends_at"]
        _set(rec["id"], airing_id=moved["id"], begins_at=moved["begins_at"],
             ends_at=moved["ends_at"], starts_at=moved["begins_at"] - lead,
             stops_at=moved["ends_at"] + trail)


def _start_due(now):
    rows = db.query(
        "SELECT * FROM recordings WHERE state IN ('scheduled', 'recording') "
        "AND starts_at <= ? AND stops_at > ? "
        # Recordings before live sessions, so a recording gets the tuner.
        "ORDER BY kind = 'recording' DESC, starts_at", (now, now))
    for rec in rows:
        if rec["id"] in _jobs:
            continue
        try:
            url = tuner.stream_url(rec["channel_vcn"])
            if rec["kind"] == "recording":
                _make_room(now)
        except tuner.TunerError as e:
            _set(rec["id"], error=str(e))
            continue
        job = Job(rec)
        _jobs[rec["id"]] = job
        job.start_capture(url)
        job.start_converter()


def _make_room(now):
    """A recording is due. If every tuner is busy, close one of our own live
    sessions. A tuner Plex holds is never taken."""
    try:
        if tuner.free() > 0:
            return
    except tuner.TunerError:
        return
    for job_id in list(_jobs):
        rec = get(job_id)
        if rec and rec["kind"] == "live":
            _end_live(rec)
            return


def _watch_running(now):
    for rec_id, job in list(_jobs.items()):
        rec = get(rec_id)
        if not rec:
            job.stop()
            _jobs.pop(rec_id, None)
            continue
        cap = job.capture
        if cap and job.capturing:
            cols = {"bytes": cap.bytes, "error": cap.error}
            if cap.connected_at and rec["state"] == "scheduled":
                cols.update(state="recording", started_at=cap.connected_at)
            _set(rec_id, **cols)
        conv = job.converter
        if conv and conv.failed() and job.convert_starts <= CONVERT_RETRIES:
            job.start_converter()
        if not job.capturing and conv and not conv.alive():
            # Capture ended earlier and the copy has now caught up and closed.
            _jobs.pop(rec_id, None)


def _stop_due(now):
    for rec_id, job in list(_jobs.items()):
        rec = get(rec_id)
        if not rec or not job.capturing or rec["stops_at"] > now:
            continue
        job.stop_capture()
        got = job.capture.bytes if job.capture else 0
        if rec["kind"] == "live":
            _end_live(rec)
        elif got or (rec["bytes"] or 0):
            _set(rec_id, state="finished", ended_at=now,
                 bytes=max(got, rec["bytes"] or 0), error=None)
        else:
            job.stop()
            _jobs.pop(rec_id, None)
            _set(rec_id, state="failed", ended_at=now,
                 error=(job.capture.error if job.capture and job.capture.error
                        else "The tuner sent nothing for the whole broadcast."))


def _close_idle_live(now):
    for rec in db.query("SELECT * FROM recordings WHERE kind = 'live'"):
        if (rec["watched_at"] or 0) < now - LIVE_IDLE_SECONDS:
            _end_live(rec)


def _end_live(rec):
    """A live session leaves nothing behind: no row, no files."""
    job = _jobs.pop(rec["id"], None)
    if job:
        job.stop()
    shutil.rmtree(rec_dir(rec["uid"]), ignore_errors=True)
    with db.tx() as c:
        c.execute("DELETE FROM recordings WHERE id = ?", (rec["id"],))


def _give_up_on_missed(now):
    """Whatever is past its end without a job is over, one way or the other.

    This is also what settles a restart: a row left 'recording' by a process
    that died has its broadcast file on disk, and is finished, not failed.
    """
    rows = db.query(
        "SELECT * FROM recordings WHERE kind = 'recording' "
        "AND state IN ('scheduled', 'recording') AND stops_at <= ?", (now,))
    for rec in rows:
        if rec["id"] in _jobs:
            continue
        try:
            size = os.path.getsize(broadcast_path(rec["uid"]))
        except OSError:
            size = 0
        if size:
            _set(rec["id"], state="finished", ended_at=rec["stops_at"], bytes=size)
        else:
            _set(rec["id"], state="failed", ended_at=now,
                 error=rec["error"] or "The recording never started.")


def ensure_view(rec, src: str | None = None) -> None:
    """Make sure a viewing copy exists or is being made for a recording that
    is no longer being captured: after a restart, or after its copy was tidied
    away. `src` is the file to read when the broadcast file has gone to Plex."""
    with _lock:
        if rec["id"] in _jobs:
            return
        if view_info(rec["uid"])["complete"]:
            return
        src = src or broadcast_path(rec["uid"])
        if not os.path.exists(src):
            return
        job = Job(rec)
        _jobs[rec["id"]] = job
        job.start_converter(src)


def busy(rec_id: int) -> bool:
    """Is a worker of ours running for this recording right now?"""
    with _lock:
        return rec_id in _jobs


def shutdown() -> None:
    """Stop every worker. For tests, and for a clean exit."""
    with _lock:
        for job in _jobs.values():
            job.stop()
        _jobs.clear()
