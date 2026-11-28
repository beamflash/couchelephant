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
    assert _starts(logf) == [["ss=2000.000000", "offset=2001.000000", "start=500"]]


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
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "encoder"), "w") as f:
        f.write(f"software/{ondemand.ENC_VERSION}")
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
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "1.5")
    t = ondemand.begin(rec, "seg00100.ts")
    for n in (300, 500, 700):
        ondemand.begin(rec, f"seg{n:05d}.ts")
    assert t.run.dead
    end = time.time() + 15
    how = ondemand.poll(t)
    while how in (ondemand.WAIT, ondemand.RESTART) and time.time() < end:
        if how == ondemand.RESTART:
            assert ondemand.restart(t)
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


def test_a_chip_failure_switches_that_recording_to_software_and_the_segment_still_comes(od, monkeypatch):
    rec, logf = od
    db.set_setting("recorder_encoder", "auto")
    monkeypatch.setattr(recorder, "_vaapi", True)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL_VAAPI", "1")
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    # This source is made in software from now on. Other recordings, and the
    # live path, are not told the chip is gone.
    assert rec["plex_path"] in ondemand._chip_bad and recorder._vaapi is True
    assert ondemand._marker(rec["uid"]) == f"software/{ondemand.ENC_VERSION}"
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


def test_a_recording_made_over_does_not_keep_the_run_of_its_old_files(od):
    import shutil
    rec, logf = od
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    ondemand.forget(rec["uid"])
    shutil.rmtree(ondemand.od_dir(rec["uid"]))
    os.makedirs(ondemand.od_dir(rec["uid"]))
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "duration"), "w") as f:
        f.write("4000.0")
    assert c.get(f"/hls/{rec['uid']}/seg00101.ts").status_code == 200
    assert len(_starts(logf)) == 2 and not ondemand.running("nothing")


def test_poll_never_starts_anything_itself(od, monkeypatch):
    rec, _ = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "1.5")
    t = ondemand.begin(rec, "seg00100.ts")
    ondemand._kill(t.run)
    monkeypatch.setattr(ondemand, "_spawn", lambda *a, **k: pytest.fail("poll spawned"))
    assert ondemand.poll(t) == ondemand.RESTART


def test_a_run_stops_when_it_meets_a_newer_one(od, monkeypatch):
    rec, _ = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.2")
    monkeypatch.setattr(ondemand, "AHEAD", 2)
    c = TestClient(app)
    assert c.get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    first = ondemand._runs[rec["uid"]][0]
    assert c.get(f"/hls/{rec['uid']}/seg00105.ts").status_code == 200
    end = time.time() + 15
    while not first.dead and time.time() < end:
        time.sleep(0.1)
    assert first.dead and len(ondemand._runs[rec["uid"]]) == 1


def test_a_run_wants_what_its_furthest_viewer_wants():
    run = ondemand._Run("u", None, 10, "software", 0, "x", 0)
    run.ask(12)
    run.ask(40)
    run.ask(13)
    assert run.want == 40


def test_a_resume_point_goes_in_the_playlist(od):
    rec, _ = od
    c = TestClient(app)
    got = c.get(f"/hls/{rec['uid']}/index.m3u8?at=200").text
    assert "#EXT-X-START:TIME-OFFSET=200.000,PRECISE=YES" in got
    assert "EXT-X-START" not in c.get(f"/hls/{rec['uid']}/index.m3u8?at=abc").text
    assert "index.m3u8?at=200" in c.get(f"/hls/{rec['uid']}/master.m3u8?at=200").text
    assert "?at" not in c.get(f"/hls/{rec['uid']}/master.m3u8?at=x").text


def _real_tools():
    import shutil
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff or not fp:
        pytest.skip("no real ffmpeg here")
    return ff, fp


def _first_video_pts(fp, path):
    import subprocess
    out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.split()
    return min(float(x.rstrip(",")) for x in out)


def _video_pts(fp, path):
    import subprocess
    out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.split()
    return sorted(float(x.rstrip(",")) for x in out)


def test_runs_from_the_start_and_after_a_seek_cut_at_the_same_pictures(tmp_path, monkeypatch):
    """Real ffmpeg, a 29.97 file whose sound leads its picture by half a
    second (as broadcasts do): segment 10 holds the same pictures, at the same
    times, in runs started at 0, 3 and 7."""
    import subprocess
    ff, fp = _real_tools()
    monkeypatch.setattr(recorder, "FFMPEG", ff)
    src = tmp_path / "lead.ts"
    subprocess.run([ff, "-v", "error", "-y", "-itsoffset", "0.5", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=30000/1001", "-f", "lavfi", "-i",
                    "sine=frequency=440", "-t", "60", "-c:v", "mpeg2video", "-c:a", "mp2",
                    "-f", "mpegts", str(src)], check=True)
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    rec = {"uid": "ab" * 16}
    os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
    v0 = ondemand.start_offset(rec, str(src))
    assert 0.3 < v0 < 0.7
    seen = {}
    for start in (0, 3, 7):
        out = tmp_path / f"r{start}"
        out.mkdir()
        proc = subprocess.Popen(recorder.convert_args("software", str(out), False, src=str(src),
                                                      start_seg=start, v0=v0),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        seg = out / "seg00010.ts"
        end = time.time() + 120
        while not seg.exists() and time.time() < end:
            time.sleep(0.1)
        proc.kill()
        proc.wait()
        assert seg.exists(), f"run at {start} made no segment 10"
        seen[start] = _video_pts(fp, str(seg))
    # Runs after a seek agree to the tenth of a millisecond. The run from the
    # start picks its first picture by the picture's own time and a seek by
    # the decoder's, which can differ by one picture: the same pictures,
    # one frame (1001/30000 s) apart, none dropped and the times continuous.
    assert len(seen[3]) == len(seen[7]) == len(seen[0])
    assert max(abs(a - b) for a, b in zip(seen[3], seen[7], strict=True)) < 0.001
    frame = 1001 / 30000
    assert max(abs(a - b) for a, b in zip(seen[0], seen[3], strict=True)) < frame + 0.001
    gaps = [b - a for a, b in zip(seen[0], seen[0][1:], strict=False)]
    assert max(gaps) < frame + 0.002 and min(gaps) > frame - 0.002




def test_a_recording_keeps_the_encoder_its_segments_were_made_with(od, monkeypatch):
    rec, _ = od
    d = ondemand.od_dir(rec["uid"])
    with open(os.path.join(d, "encoder"), "w") as f:
        f.write(f"vaapi/{ondemand.ENC_VERSION}")
    with open(os.path.join(d, "seg00005.ts"), "wb") as f:
        f.write(b"chip")
    # The machine would use software now (the setting says so), and the
    # recording stays as it was: nothing is cleared.
    assert ondemand._choose_encoder(rec["uid"], rec["plex_path"]) == "vaapi"
    assert os.path.exists(os.path.join(d, "seg00005.ts"))


def test_a_marker_from_an_older_way_of_making_segments_clears_the_folder(od):
    rec, _ = od
    d = ondemand.od_dir(rec["uid"])
    with open(os.path.join(d, "encoder"), "w") as f:
        f.write("software/1")
    with open(os.path.join(d, "seg00005.ts"), "wb") as f:
        f.write(b"old")
    assert ondemand._choose_encoder(rec["uid"], rec["plex_path"]) == "software"
    assert not os.path.exists(os.path.join(d, "seg00005.ts"))
    assert ondemand._marker(rec["uid"]) == f"software/{ondemand.ENC_VERSION}"


def test_a_chip_failure_stops_the_runs_first_and_two_requests_clear_once(od, monkeypatch):
    import threading
    rec, _ = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.05")
    assert TestClient(app).get(f"/hls/{rec['uid']}/seg00100.ts").status_code == 200
    run = ondemand._runs[rec["uid"]][0]
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "encoder"), "w") as f:
        f.write(f"vaapi/{ondemand.ENC_VERSION}")
    ondemand._chip_bad.add(rec["plex_path"])
    kills = []
    real = ondemand._kill
    monkeypatch.setattr(ondemand, "_kill", lambda r: (kills.append(r), real(r)))
    got = []
    ts = [threading.Thread(target=lambda: got.append(ondemand._choose_encoder(
        rec["uid"], rec["plex_path"]))) for _ in range(3)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert got == ["software"] * 3 and kills == [run]
    assert run.proc.poll() is not None
    assert not os.path.exists(os.path.join(ondemand.od_dir(rec["uid"]), "seg00100.ts"))


def test_a_new_run_never_inherits_the_progress_of_the_run_it_replaced(od, monkeypatch):
    rec, _ = od
    monkeypatch.setenv("FAKE_FFMPEG_DELAY", "0.02")
    c = TestClient(app)
    for n in (100, 300, 500):
        assert c.get(f"/hls/{rec['uid']}/seg{n:05d}.ts").status_code == 200
        time.sleep(0.02)
    time.sleep(1.0)                         # they run on, and make progress
    first = ondemand._runs[rec["uid"]][0]
    names = {r.playlist for r in ondemand._runs[rec["uid"]]}
    assert c.get(f"/hls/{rec['uid']}/seg00700.ts").status_code == 200
    new = ondemand._runs[rec["uid"]][-1]
    assert first.dead and new.start == 700 and new.playlist not in names
    assert not os.path.exists(os.path.join(ondemand.od_dir(rec["uid"]), first.playlist))
    assert new.progress() >= 700 and new.progress() < 760


def test_a_start_time_the_file_does_not_give_is_settled_as_zero(tmp_path, monkeypatch):
    fake = tmp_path / "ffprobe"
    fake.write_text("#!/bin/sh\necho N/A\n")
    fake.chmod(0o755)
    monkeypatch.setattr(recorder, "FFPROBE", str(fake))
    rec = {"uid": "cd" * 16}
    assert ondemand.start_offset(rec, "/x") == 0.0
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "v0")) as f:
        assert float(f.read()) == 0.0


def _streams(fp, path):
    import json
    import subprocess
    out = subprocess.run([fp, "-v", "error", "-show_entries", "stream=codec_name,channels",
                          "-of", "json", path], capture_output=True, text=True).stdout
    return [",".join(str(x) for x in (s["codec_name"], s.get("channels")) if x)
            for s in json.loads(out)["streams"]]


def _audio_pts(fp, path, track):
    import subprocess
    out = subprocess.run([fp, "-v", "error", "-select_streams", f"a:{track}", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.split()
    return [float(x.rstrip(",")) for x in out]


def _surround_source(ff, path, codec="ac3", seconds=30):
    import subprocess
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=30000/1001", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-filter_complex", "[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a]",
                    "-map", "0:v", "-map", "[a]", "-t", str(seconds),
                    "-c:v", "mpeg2video", "-c:a", codec, "-f", "mpegts", str(path)], check=True)


def _segment(ff, src, out, start=0, surround=True, v0=0.0, n=None):
    import subprocess
    out.mkdir()
    proc = subprocess.Popen(recorder.convert_args("software", str(out), False, src=str(src),
                                                  start_seg=start, v0=v0, surround=surround),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    seg = out / f"seg{(start if n is None else n):05d}.ts"
    end = time.time() + 120
    while not seg.exists() and time.time() < end:
        time.sleep(0.1)
    proc.kill()
    proc.wait()
    assert seg.exists(), f"no {seg.name}"
    return str(seg)


def test_a_5_1_broadcast_keeps_its_own_sound_first_and_stereo_second(tmp_path, monkeypatch):
    """Apple's player and Safari play the first track they can (5.1 on a
    receiver); a browser without AC-3 skips to the stereo one."""
    ff, fp = _real_tools()
    monkeypatch.setattr(recorder, "FFMPEG", ff)
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    src = tmp_path / "game.ts"
    _surround_source(ff, src)
    assert recorder.audio_codec(str(src)) == "ac3"
    assert recorder.keeps_surround(str(src))
    seg = _segment(ff, src, tmp_path / "out")
    assert _streams(fp, seg) == ["h264", "ac3,6", "aac,2"]


def test_sound_that_is_not_ac3_gets_the_stereo_track_alone(tmp_path, monkeypatch):
    import subprocess
    ff, fp = _real_tools()
    monkeypatch.setattr(recorder, "FFMPEG", ff)
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    src = tmp_path / "iptv.ts"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "10", "-c:v", "mpeg2video",
                    "-c:a", "mp2", "-f", "mpegts", str(src)], check=True)
    assert recorder.audio_codec(str(src)) == "mp2"
    assert not recorder.keeps_surround(str(src))
    rec = {"uid": "cd" * 16}
    os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
    assert ondemand.surround(rec, str(src)) is False
    seg = _segment(ff, src, tmp_path / "out", surround=False)
    assert _streams(fp, seg) == ["h264", "aac,2"]


def test_on_demand_runs_put_the_copied_sound_at_the_same_times(tmp_path, monkeypatch):
    """Segments from runs started at different places join without a jump."""
    ff, fp = _real_tools()
    monkeypatch.setattr(recorder, "FFMPEG", ff)
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    src = tmp_path / "game.ts"
    _surround_source(ff, src, seconds=60)
    rec = {"uid": "ef" * 16}
    os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
    assert ondemand.surround(rec, str(src)) is True
    # Cached: the answer holds without ffprobe.
    monkeypatch.setattr(recorder, "FFPROBE", "/nonexistent/ffprobe")
    assert ondemand.surround(rec, str(src)) is True
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    a = _segment(ff, src, tmp_path / "r3", start=3, n=8)
    b = _segment(ff, src, tmp_path / "r6", start=6, n=8)
    # The copied track is the broadcast's own frames: the same times exactly.
    pa, pb = _audio_pts(fp, a, 0), _audio_pts(fp, b, 0)
    assert pa and pa == pb
    # The stereo track is encoded per run and was always within one AAC
    # frame (1024 samples) of another run's, as before this track existed.
    pa, pb = _audio_pts(fp, a, 1), _audio_pts(fp, b, 1)
    assert pa and pb and abs(pa[0] - pb[0]) < 1024 / 48000


def test_a_recording_ffprobe_cannot_read_stays_stereo(tmp_path, monkeypatch):
    """Cached as it is: a later answer of "ac3" would put two-track segments
    beside the stereo ones already made."""
    monkeypatch.setattr(recorder, "FFPROBE", "/nonexistent/ffprobe")
    rec = {"uid": "fa" * 16}
    os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
    (tmp_path / "b.ts").write_bytes(b"x" * 10)
    assert ondemand.surround(rec, str(tmp_path / "b.ts")) is False
    monkeypatch.setattr(recorder, "audio_codec", lambda p: "ac3")
    assert ondemand.surround(rec, str(tmp_path / "b.ts")) is False
    assert not [n for n in os.listdir(ondemand.od_dir(rec["uid"])) if n.endswith(".part")]


def test_e_ac3_is_made_stereo_alone():
    """The web player's hls.js stops at an E-AC-3 track and never reaches the
    stereo one after it: picture, no sound."""
    assert "eac3" not in recorder.SURROUND and "ac3" in recorder.SURROUND


def test_a_segment_made_the_old_way_is_never_served(od):
    """After an upgrade a half-watched recording must not play old stereo
    segments next to new two-track ones."""
    rec, _ = od
    old = os.path.join(ondemand.od_dir(rec["uid"]), "seg00000.ts")
    with open(old, "wb") as f:
        f.write(b"old")
    with open(os.path.join(ondemand.od_dir(rec["uid"]), "encoder"), "w") as f:
        f.write(f"software/{ondemand.ENC_VERSION - 1}")
    got = TestClient(app).get(f"/hls/{rec['uid']}/seg00000.ts")
    assert got.status_code == 200 and got.content != b"old"


def test_a_run_after_a_seek_never_repeats_the_sound_before_it(tmp_path, monkeypatch):
    """Where a run started at a seek meets the segment before it (made by
    another run), the copied sound must not go back in time; a frame or two
    of silence is the most it may leave."""
    import subprocess
    ff, fp = _real_tools()
    monkeypatch.setattr(recorder, "FFMPEG", ff)
    monkeypatch.setattr(recorder, "FFPROBE", fp)
    frame = 1536 / 48000
    for lead in ("0", "0.5"):
        src = tmp_path / f"lead{lead}.ts"
        subprocess.run([ff, "-v", "error", "-y", "-itsoffset", lead, "-f", "lavfi", "-i",
                        "testsrc=size=320x180:rate=30000/1001", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000", "-filter_complex",
                        "[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a]",
                        "-map", "0:v", "-map", "[a]", "-t", "40", "-c:v", "mpeg2video",
                        "-c:a", "ac3", "-f", "mpegts", str(src)], check=True)
        rec = {"uid": ("1" if lead == "0" else "2") * 32}
        os.makedirs(ondemand.od_dir(rec["uid"]), exist_ok=True)
        v0 = ondemand.start_offset(rec, str(src))
        before = _segment(ff, src, tmp_path / f"a{lead}", start=3, n=4, v0=v0)
        after = _segment(ff, src, tmp_path / f"b{lead}", start=5, n=5, v0=v0)
        end = _audio_pts(fp, before, 0)[-1] + frame
        first = _audio_pts(fp, after, 0)[0]
        assert end - 0.001 <= first <= end + 2 * frame + 0.001, (lead, end, first)


def test_on_demand_segment_addresses_carry_the_version(od):
    """A player keeps a segment for a day: a stereo one cached before the
    upgrade must not be taken for a new one."""
    rec, _ = od
    text = ondemand.playlist(rec)
    assert f"seg00000.ts?v={ondemand.ENC_VERSION}" in text
    got = TestClient(app).get(f"/hls/{rec['uid']}/seg00000.ts?v={ondemand.ENC_VERSION}")
    assert got.status_code == 200
