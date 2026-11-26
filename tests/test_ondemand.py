"""A recording in Plex without its viewing copy: every segment is listed at
once, and a segment is made when it is asked for."""
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import db, ondemand, recorder
from app.web import app
from tests.test_recorder import _record

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_ffmpeg_od.py")


@pytest.fixture
def od(tuner, tmp_path, monkeypatch):
    """A finished recording moved to Plex, its copy gone, and a stand-in ffmpeg."""
    rec = _record()
    src = tmp_path / "game.ts"
    src.write_bytes(b"x" * 100)
    with db.tx() as c:
        c.execute("UPDATE recordings SET state = 'in_plex', plex_path = ? WHERE id = ?",
                  (str(src), rec["id"]))
    import shutil
    shutil.rmtree(recorder.view_dir(rec["uid"]), ignore_errors=True)
    wrapper = tmp_path / "ffmpeg"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    logf = tmp_path / "ff.log"
    monkeypatch.setattr(recorder, "FFMPEG", str(wrapper))
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(logf))
    ondemand.allow()
    db.set_setting("recorder_encoder", "software")
    os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "duration"), "w") as f:
        f.write("4000.0")          # 1000 segments of 4 s
    yield recorder.get(rec["id"]), logf
    ondemand.shutdown()


def _starts(logf):
    return [line.split()[1:] for line in logf.read_text().splitlines()] if logf.exists() else []


def test_the_playlist_lists_the_whole_duration(od):
    rec, _ = od
    got = TestClient(app).get(f"/hls/{rec['uid']}/index.m3u8")
    assert got.status_code == 200
    t = got.text
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in t and t.rstrip().endswith("#EXT-X-ENDLIST")
    assert t.count("#EXTINF") == 1000 and "seg00999.ts" in t
    assert TestClient(app).get(f"/hls/{rec['uid']}/master.m3u8").status_code == 200


def test_a_far_segment_starts_ffmpeg_at_its_time(od):
    rec, logf = od
    got = TestClient(app).get(f"/hls/{rec['uid']}/seg00500.ts")
    assert got.status_code == 200 and got.content == b"ts500"
    assert _starts(logf) == [["ss=2000", "offset=2000", "start=500"]]


def test_two_requests_do_not_start_two_ffmpegs(od):
    rec, logf = od
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    # Just ahead of what it has made: waited for, not restarted.
    assert c.get(f"/hls/{rec['uid']}/seg00103.ts").status_code == 200
    assert len(_starts(logf)) == 1
    # Far away: a second viewer's run starts there, the first goes on.
    assert c.get(f"/hls/{rec['uid']}/seg00700.ts").status_code == 200
    assert len(_starts(logf)) == 2 and _starts(logf)[1][2] == "start=700"
    assert len(ondemand._runs[rec["uid"]]) == 2


def test_an_idle_ffmpeg_is_stopped(od, monkeypatch):
    rec, _ = od
    TestClient(app).get(f"/hls/{rec['uid']}/seg00010.ts")
    proc = ondemand._runs[rec["uid"]][0].proc
    monkeypatch.setattr(ondemand, "IDLE_SECONDS", 0.1)
    deadline = time.time() + 8
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    assert proc.poll() is not None and not ondemand._runs


def test_a_segment_past_the_end_is_not_there(od):
    rec, _ = od
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg01000.ts").status_code == 404


def test_a_segment_left_by_an_earlier_run_is_not_this_runs_progress(od, monkeypatch):
    rec, logf = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.5")
    # Segments 101 to 130 are on disk from a run that is gone.
    for n in range(101, 131):
        p = os.path.join(ondemand.od_dir(rec["uid"]), f"seg{n:05d}.ts")
        with open(p, "wb") as f:
            f.write(b"old")
        os.utime(p, (1, 1))
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    assert c.get(f"/hls/{rec['uid']}/seg00125.ts").content == b"old"
    # The run at 100 has made 100 only: 131 is nowhere near it.
    assert c.get(f"/hls/{rec['uid']}/seg00131.ts").status_code == 200
    assert [s[2] for s in _starts(logf)] == ["start=100", "start=131"]


def test_the_recording_says_all_of_it_is_there_before_its_stream_was_ever_asked(od):
    rec, _ = od
    os.remove(os.path.join(ondemand.od_dir(rec["uid"]), "duration"))
    got = TestClient(app).get(f"/api/tv/recordings/{rec['id']}").json()["recording"]
    assert got["complete"] is True and got["playable"] is True and got["seconds"] > 0


def _viewer(rec, first, count, results):
    c = TestClient(app)
    for n in range(first, first + count):
        results.append((n, c.get(f"/hls/{rec['uid']}/seg{n:05d}.ts").status_code))


def test_two_viewers_far_apart_each_keep_their_own_ffmpeg(od, monkeypatch):
    import threading
    rec, logf = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.1")
    a, b = [], []
    ts = [threading.Thread(target=_viewer, args=(rec, 100, 12, a)),
          threading.Thread(target=_viewer, args=(rec, 700, 12, b))]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert all(code == 200 for _, code in a + b) and len(a) == len(b) == 12
    assert sorted(s[2] for s in _starts(logf)) == ["start=100", "start=700"]
    assert len(ondemand._runs[rec["uid"]]) == 2


def test_the_least_recently_asked_run_is_stopped_over_the_cap(od):
    rec, logf = od
    c = TestClient(app)
    for n in (100, 300, 500):
        assert c.get(f"/hls/{rec['uid']}/seg{n:05d}.ts").status_code == 200
        time.sleep(0.02)
    first = ondemand._runs[rec["uid"]][0]
    assert c.get(f"/hls/{rec['uid']}/seg00700.ts").status_code == 200
    assert len(ondemand._runs[rec["uid"]]) == ondemand.MAX_RUNS
    assert first.dead and first.proc.poll() is not None


def test_a_request_whose_run_was_replaced_starts_again(od, monkeypatch):
    rec, logf = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.3")
    t = ondemand.begin(rec, "seg00100.ts")
    for n in (300, 500, 700):
        ondemand.begin(rec, f"seg{n:05d}.ts")
    assert t.run.dead
    end = time.time() + 15
    how = ondemand.poll(t)
    while how == ondemand.WAIT and time.time() < end:
        time.sleep(0.05)
        how = ondemand.poll(t)
    assert how == ondemand.READY and t.restarts >= 1


def _state(pid):
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(")", 1)[1].split()[0]


def test_a_run_far_ahead_of_its_viewer_is_paused_and_goes_on_when_he_catches_up(od, monkeypatch):
    rec, _ = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.01")
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg00010.ts").status_code == 200
    run = ondemand._runs[rec["uid"]][0]
    end = time.time() + 15
    while not run.paused and time.time() < end:
        time.sleep(0.1)
    assert run.paused and _state(run.proc.pid) == "T"
    ahead = run.progress()
    assert ahead - 10 > ondemand.RESUME_AT
    # He gets to within a minute of it: it goes on.
    near = ahead - ondemand.RESUME_AT + 1
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg{near:05d}.ts").status_code == 200
    assert not run.paused and _state(run.proc.pid) != "T"


def test_a_failure_that_is_not_the_chips_leaves_the_chip_on(od, monkeypatch):
    rec, logf = od
    db.set_setting("recorder_encoder", "auto")
    monkeypatch.setattr(recorder, "_vaapi", True)
    monkeypatch.setenv("FAKE_FFMPEG_EXIT", "1")
    monkeypatch.setenv("FAKE_FFMPEG_SAYS", "Invalid data found when processing input")
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 404
    assert recorder._vaapi is True
    # Remembered: asked again at once, nothing is started.
    t0 = time.time()
    assert c.get(f"/hls/{rec['uid']}/seg00101.ts").status_code == 404
    assert time.time() - t0 < 2 and len(_starts(logf)) == 1


def test_a_chip_failure_switches_to_software_and_the_segment_still_comes(od, monkeypatch):
    rec, logf = od
    db.set_setting("recorder_encoder", "auto")
    monkeypatch.setattr(recorder, "_vaapi", True)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL_VAAPI", "1")
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    assert recorder._vaapi is False
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "encoder")) as f:
        assert f.read().strip() == "software"
    # Said in ffmpeg.log, and kept, not cut, by the next start.
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "ffmpeg-0.log")) as f:
        assert "VAAPI" in f.read()


def test_a_changed_encoder_clears_what_the_other_made(od):
    rec, _ = od
    d = ondemand.od_dir(rec["uid"])
    with open(os.path.join(d, "encoder"), "w") as f:
        f.write("vaapi")
    with open(os.path.join(d, "seg00005.ts"), "wb") as f:
        f.write(b"chip")
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    assert not os.path.exists(os.path.join(d, "seg00005.ts"))
    assert os.path.exists(os.path.join(d, "duration"))


def test_a_missing_file_fails_at_once_and_starts_nothing(od):
    rec, logf = od
    os.remove(rec["plex_path"])
    if os.path.exists(recorder.broadcast_path(rec["uid"])):
        os.remove(recorder.broadcast_path(rec["uid"]))
    assert ondemand.begin(rec, "seg00100.ts") is None
    assert not logf.exists()
    got = TestClient(app).get(f"/api/tv/recordings/{rec['id']}").json()["recording"]
    assert got["playable"] is False and "not in Plex" in got["note"]


def test_nothing_starts_after_shutdown(od):
    rec, logf = od
    ondemand.shutdown()
    assert ondemand.begin(rec, "seg00100.ts") is None
    assert not logf.exists()


def test_a_recording_being_watched_is_not_tidied(od):
    from app import handover
    rec, _ = od
    with db.tx() as c:
        c.execute("UPDATE recordings SET moved_at = 1 WHERE id = ?", (rec["id"],))
    TestClient(app).get(f"/hls/{rec['uid']}/seg00100.ts")
    handover.tidy()
    assert os.path.exists(ondemand.od_dir(rec["uid"]))
    ondemand.shutdown()
    handover.tidy()
    assert not os.path.exists(ondemand.od_dir(rec["uid"]))


def test_only_a_plain_segment_name_is_a_segment(od):
    rec, _ = od
    c = TestClient(app)
    for bad in ("seg1000.ts", "seg\u0661\u0662\u0663\u0664\u0665.ts", "seg00001.ts%0a"):
        assert c.get(f"/hls/{rec['uid']}/{bad}").status_code == 404
