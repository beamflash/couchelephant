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
    # Far away: the first is stopped and one starts there.
    assert c.get(f"/hls/{rec['uid']}/seg00700.ts").status_code == 200
    assert len(_starts(logf)) == 2 and _starts(logf)[1][2] == "start=700"
    assert len(ondemand._runs) == 1


def test_an_idle_ffmpeg_is_stopped(od, monkeypatch):
    rec, _ = od
    TestClient(app).get(f"/hls/{rec['uid']}/seg00010.ts")
    proc = ondemand._runs[rec["uid"]].proc
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
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00300.ts").status_code == 200
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    # 300 is still on disk from the first run, and the run at 100 is far from
    # it: asking for 301 starts there and does not wait for 100 to climb.
    assert c.get(f"/hls/{rec['uid']}/seg00301.ts").status_code == 200
    assert [s[2] for s in _starts(logf)] == ["start=300", "start=100", "start=301"]


def test_the_recording_says_all_of_it_is_there_before_its_stream_was_ever_asked(od):
    rec, _ = od
    os.remove(os.path.join(ondemand.od_dir(rec["uid"]), "duration"))
    got = TestClient(app).get(f"/api/tv/recordings/{rec['id']}").json()["recording"]
    assert got["complete"] is True and got["playable"] is True and got["seconds"] > 0
