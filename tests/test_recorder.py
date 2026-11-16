"""CouchElephant's own recorder: the tuner, the two files, the handover.

Driven against a fake HDHomeRun over real HTTP and a stand-in for ffmpeg, so
the capture, the feeding of a growing file and the state machine all run for
real and only the codecs are missing.
"""
import json
import os
import time

import pytest

from app import db, handover, passes, recorder
from app import tuner as tuner_mod
from tests import fake_plex


def _airing(aid, guid, vcn, begins, length=3600, title="Chiefs at Broncos",
            show="NFL Football", ptype="episode", season=None, episode=None,
            year=None, team=236):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section, type, season, episode, year) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (guid, title, "rk", json.dumps([{"id": team, "name": "Chiefs"}]),
                   show, "sports", ptype, season, episode, year))
        c.execute("INSERT OR IGNORE INTO channels (vcn, network, call_sign) VALUES (?,?,?)",
                  (vcn, "CBS", vcn))
        c.execute("""INSERT OR REPLACE INTO airings
                     (id, program_guid, channel_vcn, channel_identifier, channel_title,
                      begins_at, ends_at, premiere, drm) VALUES (?,?,?,?,?,?,?,1,0)""",
                  (aid, guid, vcn, f"id-{vcn}", f"{vcn} KCTV", begins, begins + length))
    return db.one("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams
                     FROM airings a JOIN programs p ON p.guid = a.program_guid
                     WHERE a.id = ?""", (aid,))


def _until(what, seconds=10):
    """Wait for something the workers do in their own time."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = what()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("it never happened")


def _record(vcn="5.1", **kw):
    """A broadcast that is on now, booked, captured and ended. The finished row."""
    now = int(time.time())
    rec_id = recorder.book(_airing("a-" + vcn, "g-" + vcn, vcn, now - 60, **kw))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    _until(lambda: recorder.view_info(recorder.get(rec_id)["uid"])["segments"] > 0)
    recorder.tick(now=recorder.get(rec_id)["stops_at"] + 1)
    _until(lambda: (recorder.tick() or not recorder.busy(rec_id)))
    return recorder.get(rec_id)


# ---------- the tuner ----------

def test_the_tuner_says_how_many_are_free(tuner):
    tuner.plex_holds = ["9.1"]
    assert tuner_mod.total() == 4
    assert tuner_mod.free() == 3


def test_an_encrypted_or_atsc3_channel_is_refused_in_words(tuner):
    tuner.extra = [{"GuideNumber": "105.1", "DRM": 1, "URL": "x"},
                   {"GuideNumber": "109.1", "AudioCodec": "AC4", "URL": "x"}]
    with pytest.raises(tuner_mod.TunerError, match="encrypted"):
        tuner_mod.stream_url("105.1")
    with pytest.raises(tuner_mod.TunerError, match="ATSC 3.0"):
        tuner_mod.stream_url("109.1")


def test_no_tuner_address_is_said_plainly(clean_db):
    with pytest.raises(tuner_mod.TunerError, match="No tuner address"):
        tuner_mod.lineup()


# ---------- a recording, start to end ----------

def test_a_recording_can_be_watched_from_the_start_while_it_records(tuner):
    """The reason any of this exists."""
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    assert recorder.get(rec_id)["state"] == "scheduled"
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    uid = recorder.get(rec_id)["uid"]
    view = _until(lambda: (v := recorder.view_info(uid))["segments"] > 1 and v)
    # Still growing, and the first segment is still there to start from.
    assert not view["complete"]
    assert os.path.exists(os.path.join(recorder.view_dir(uid), "seg00000.ts"))
    assert tuner.streams == ["5.1"]


def test_the_end_closes_both_files_and_frees_the_tuner(tuner):
    rec = _record()
    assert rec["state"] == "finished"
    assert rec["bytes"] > 0
    assert os.path.getsize(recorder.broadcast_path(rec["uid"])) == rec["bytes"]
    assert recorder.view_info(rec["uid"])["complete"]
    _until(lambda: not tuner.live)


def test_the_viewing_copy_holds_every_byte_of_the_broadcast(tuner):
    """The converter reads a file that is still being written. Nothing at the
    end of it may be lost to the moment the capture stops."""
    rec = _record()
    view = recorder.view_dir(rec["uid"])
    copied = sum(os.path.getsize(os.path.join(view, f))
                 for f in os.listdir(view) if f.startswith("seg"))
    assert copied == rec["bytes"]


def test_every_tuner_busy_is_a_reason_and_then_a_failure(tuner):
    tuner.plex_holds = ["4.1", "9.1", "38.1", "41.1"]
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["error"]))
    rec = recorder.get(rec_id)
    assert rec["state"] == "scheduled"            # still trying
    assert rec["error"] == "Every tuner is in use."
    recorder.tick(now=rec["stops_at"] + 1)
    rec = recorder.get(rec_id)
    assert rec["state"] == "failed"
    assert rec["error"] == "Every tuner is in use."


def test_a_late_start_is_a_short_recording_not_none(tuner):
    tuner.plex_holds = ["4.1", "9.1", "38.1", "41.1"]
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["error"]))
    tuner.plex_holds = []                         # Plex lets one go
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    assert recorder.get(rec_id)["error"] is None


def test_booking_the_same_broadcast_twice_is_one_recording(tuner):
    now = int(time.time())
    a = _airing("a1", "g1", "5.1", now + 3600)
    assert recorder.book(a) == recorder.book(a)
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 1


def test_the_guide_naming_a_generic_slot_renames_the_one_recording(tuner):
    now = int(time.time())
    first = recorder.book(_airing("generic", "g-generic", "5.1", now + 3600,
                                  title="NFL Football"))
    named = _airing("named", "g-named", "5.1", now + 3600, title="Chiefs at Broncos")
    assert recorder.book(named) == first
    rec = recorder.get(first)
    assert (rec["title"], rec["program_guid"]) == ("Chiefs at Broncos", "g-named")


def test_a_recording_follows_a_broadcast_the_guide_re_times(tuner):
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now + 3600))
    before = recorder.get(rec_id)
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE id = 'a1'")
    _airing("a1-moved", "g1", "5.1", now + 5400)
    recorder.tick()
    rec = recorder.get(rec_id)
    assert rec["begins_at"] == now + 5400
    # The padding it was booked with goes with it.
    assert rec["begins_at"] - rec["starts_at"] == before["begins_at"] - before["starts_at"]
    assert rec["stops_at"] - rec["ends_at"] == before["stops_at"] - before["ends_at"]


def test_cancel_stops_the_tuner_and_leaves_nothing(tuner):
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    uid = recorder.get(rec_id)["uid"]
    recorder.cancel(rec_id)
    assert recorder.get(rec_id) is None
    assert not os.path.exists(recorder.rec_dir(uid))
    _until(lambda: not tuner.live)


def test_a_restart_mid_recording_keeps_what_was_recorded(tuner):
    """A process that died left a row saying 'recording' and a file on disk.
    That is a finished recording, not a failed one."""
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 7200))
    uid = recorder.get(rec_id)["uid"]
    os.makedirs(recorder.rec_dir(uid))
    with open(recorder.broadcast_path(uid), "wb") as f:
        f.write(b"x" * 1000)
    recorder._set(rec_id, state="recording")
    recorder.tick()
    rec = recorder.get(rec_id)
    assert (rec["state"], rec["bytes"]) == ("finished", 1000)


# ---------- live viewing ----------

def test_watching_a_channel_that_is_being_recorded_uses_the_recording(tuner):
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    assert recorder.start_live("5.1") == rec_id
    assert tuner.streams == ["5.1"]               # no second tuner


def test_a_recording_takes_the_tuner_from_our_own_live_viewing(tuner):
    tuner.total = 1
    live = recorder.start_live("9.1", "News")
    _until(lambda: tuner.live == ["9.1"])
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    assert recorder.get(live) is None             # a live session leaves no row
    assert tuner.live == ["5.1"]


def test_a_tuner_plex_holds_is_never_taken(tuner):
    tuner.total = 1
    tuner.plex_holds = ["9.1"]
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["error"]))
    assert tuner.plex_holds == ["9.1"]
    assert recorder.get(rec_id)["state"] == "scheduled"


def test_live_viewing_nobody_keeps_alive_is_closed_after_the_pause_limit(tuner):
    """A paused television whose app went to sleep sends nothing. Its channel
    is held for the pause limit, so it comes back to where it stopped."""
    db.set_setting("live_pause_minutes", "10")
    live = recorder.start_live("9.1", "News")
    _until(lambda: tuner.live == ["9.1"])
    recorder.tick(now=int(time.time()) + 5 * 60)
    assert recorder.get(live) is not None, "closed before the pause limit"
    recorder.tick(now=int(time.time()) + 10 * 60 + 5)
    assert recorder.get(live) is None
    _until(lambda: not tuner.live)


def test_a_closed_player_gives_the_tuner_back_at_once(tuner):
    live = recorder.start_live("9.1", "News")
    _until(lambda: tuner.live == ["9.1"])
    recorder.stop_live(live)
    assert recorder.get(live) is None
    _until(lambda: not tuner.live)


def test_the_pause_limit_stays_between_5_and_240_minutes():
    for said, seconds in (("1", 300), ("45", 2700), ("9999", 14400), ("nonsense", 1800)):
        db.set_setting("live_pause_minutes", said)
        assert recorder.pause_limit() == seconds


# ---------- a pass that names our recorder ----------

def _pass(who):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, recorder, enabled, "
                  "created_at) VALUES ('team', 236, 'Chiefs', 'u1', ?, 1, 0)", (who,))


def test_a_pass_set_to_couchelephant_books_here_and_not_in_plex(plex, tuner):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 3600)
    _pass("couchelephant")
    out = passes.run_passes()
    assert [d["action"] for d in out] == ["scheduled"]
    assert fake_plex.STATE.created == []
    rec = db.one("SELECT * FROM recordings")
    assert (rec["airing_id"], rec["source"], rec["pass_uid"]) == ("a1", "pass", "u1")
    # And the next run knows.
    again = passes.run_passes()
    assert again[0]["reason"] == "CouchElephant is recording it"
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 1


def test_a_pass_honours_plex_padding_with_our_recorder(plex, tuner):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 3600)
    _pass("couchelephant")
    with db.tx() as c:
        c.execute("UPDATE passes SET prefs = ?",
                  (json.dumps({"startOffsetMinutes": "5", "endOffsetMinutes": "30"}),))
    passes.run_passes()
    rec = db.one("SELECT * FROM recordings")
    assert rec["begins_at"] - rec["starts_at"] == 300
    assert rec["stops_at"] - rec["ends_at"] == 1800


def test_a_pass_without_the_choice_still_books_in_plex(plex, tuner):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 3600)
    _pass(None)
    passes.run_passes()
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 0


def test_a_pass_never_falls_back_to_plex_when_the_recorder_is_off(plex, tuner, monkeypatch):
    """The user chose. A silent switch would hide that the recorder is off."""
    monkeypatch.setattr(recorder, "REC_DIR", "/nonexistent-ce-test")
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 3600)
    _pass("couchelephant")
    out = passes.run_passes()
    assert out[0]["action"] == "failed"
    assert "no /nonexistent-ce-test folder" in out[0]["reason"]
    assert fake_plex.STATE.created == []


# ---------- the handover to Plex ----------

@pytest.fixture
def library(plex):
    db.set_setting("recorder_shows_folder", "tv")
    db.set_setting("recorder_shows_section", "2")
    db.set_setting("recorder_movies_folder", "films")
    db.set_setting("recorder_movies_section", "1")
    os.makedirs(os.path.join(handover.LIB_DIR, "tv"))
    return os.path.join(handover.LIB_DIR, "tv")


def test_a_finished_recording_goes_to_plex_and_stays_playable(tuner, library):
    rec = _record()
    recorder._set(rec["id"], position=1500.0, watched_at=0)
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex", done["error"]
    assert done["plex_path"].startswith(library)
    assert os.path.getsize(done["plex_path"]) == rec["bytes"]
    # Plex was asked to scan the one folder, as Plex sees it.
    section, path = fake_plex.STATE.scans[-1]
    assert section == "2"
    assert path == "/media/video/tv/" + os.path.relpath(
        os.path.dirname(done["plex_path"]), library)
    # The position went with it.
    assert fake_plex.STATE.positions[done["plex_rating_key"]] == 1_500_000
    # Ours is gone, and the copy you watch is not.
    assert not os.path.exists(recorder.broadcast_path(rec["uid"]))
    assert recorder.view_info(rec["uid"])["complete"]


def test_nothing_is_moved_while_somebody_is_watching(tuner, library):
    rec = _record()
    recorder.watching(rec["id"], 30)
    handover.tick(wait=True)
    assert recorder.get(rec["id"])["state"] == "finished"
    assert fake_plex.STATE.scans == []


def test_our_copy_is_kept_until_plex_lists_the_file(tuner, library, monkeypatch):
    monkeypatch.setattr(handover, "SCAN_WAIT_SECONDS", 0)
    fake_plex.STATE.scan_blind = True
    rec = _record()
    handover.tick(wait=True)
    held = recorder.get(rec["id"])
    assert held["state"] == "finished"
    assert "has not listed it yet" in held["error"]
    assert os.path.exists(recorder.broadcast_path(rec["uid"]))
    # Not tried again straight away, and when it is, not copied again.
    handover.tick(wait=True)
    assert len(fake_plex.STATE.scans) == 1
    fake_plex.STATE.scan_blind = False
    handover.tick(now=int(time.time()) + handover.RETRY_SECONDS + 1, wait=True)
    assert recorder.get(rec["id"])["state"] == "in_plex"


def test_a_handover_that_is_not_set_up_holds_the_recording_and_says_why(tuner, plex):
    rec = _record()
    handover.tick(wait=True)
    held = recorder.get(rec["id"])
    assert held["state"] == "finished"
    assert "No library folder is chosen for shows" in held["error"]
    assert "CouchElephant DVR" in held["error"], "it names the settings tab to go to"
    assert os.path.exists(recorder.broadcast_path(rec["uid"]))


def test_an_old_viewing_copy_is_removed_and_made_again_on_demand(tuner, library):
    rec = _record()
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    handover.tidy(now=int(time.time()) + 3 * 86400)
    assert not os.path.exists(recorder.view_dir(rec["uid"]))
    recorder.ensure_view(done, done["plex_path"])
    _until(lambda: recorder.view_info(rec["uid"])["complete"])


def test_a_recording_deleted_in_plex_leaves_the_list(tuner, library):
    rec = _record()
    handover.tick(wait=True)
    os.remove(recorder.get(rec["id"])["plex_path"])
    handover.tidy(now=int(time.time()) + 3 * 86400)
    assert recorder.get(rec["id"]) is None


# ---------- the name Plex is given ----------

def _named(**kw):
    base = {"title": "Pilot", "show_title": "The Show", "ptype": "episode",
            "season": None, "episode": None, "year": None,
            "begins_at": 1794700800}              # 2026-11-15 00:00 UTC
    base.update(kw)
    return base


def test_an_episode_with_numbers_is_named_the_way_plex_names_it():
    assert handover.relative_path(_named(season=2, episode=5)) == \
        "The Show/Season 02/The Show - S02E05 - Pilot.ts"


def test_sport_is_named_by_the_day_it_aired():
    got = handover.relative_path(_named(title="Chiefs at Broncos",
                                        show_title="NFL Football"))
    assert got == "NFL Football/Season 2026/NFL Football - 2026-11-15 - Chiefs at Broncos.ts"


def test_a_film_gets_its_own_folder_with_the_year():
    got = handover.relative_path(_named(title="Heat", show_title=None,
                                        ptype="movie", year=1995))
    assert got == "Heat (1995)/Heat (1995).ts"


def test_characters_a_filesystem_refuses_are_replaced_not_dropped():
    got = handover.relative_path(_named(title='Who? What: "Now"/Then',
                                        show_title="A/B"))
    assert got == "A B/Season 2026/A B - 2026-11-15 - Who What Now Then.ts"
    assert handover.clean('???') == "Untitled"


def test_a_show_plex_already_has_keeps_its_folder(tmp_path):
    (tmp_path / "The Show (2019)").mkdir()
    got = handover.relative_path(_named(season=1, episode=1), str(tmp_path))
    assert got == "The Show (2019)/Season 01/The Show (2019) - S01E01 - Pilot.ts"


# ---------- the encoder ----------

def test_the_hardware_probe_asks_for_exactly_what_a_conversion_asks_for():
    """A probe that passes on easier settings than the real job is a lie. On a
    live install it passed, and every recording then failed to convert."""
    for laced in (False, True):
        args = recorder.convert_args("vaapi", "/x", laced)
        for word in recorder.VAAPI_ENCODE:
            assert word in args
        # Constant quality. The free Intel driver refuses a bitrate target.
        assert "-b:v" not in args and "-maxrate" not in args
    # A progressive channel is not deinterlaced at all: at field rate it came
    # out at twice its frame rate with every caption doubled.
    progressive = recorder.convert_args("vaapi", "/x", False)
    assert recorder.VAAPI_UPLOAD in progressive
    assert not any("deinterlace" in a for a in progressive)
    # The probe runs the harder of the two, the graph that keeps captions single.
    assert recorder.VAAPI_FILTER in recorder.VAAPI_CAPTIONS_ONCE


def test_an_interlaced_channel_on_hardware_gets_its_captions_once():
    """Field rate made two pictures carrying the same captions, and every
    letter came out twice. The second of each pair has them taken off."""
    args = recorder.convert_args("vaapi", "/x", True)
    graph = args[args.index("-filter_complex") + 1]
    assert graph == recorder.VAAPI_CAPTIONS_ONCE
    assert "sidedata=mode=delete:type=A53_CC" in graph
    assert args[args.index("[v]") - 1] == "-map"
    # Without it HLS output ran at 25 pictures a second (found on a live channel).
    assert args[args.index("-fps_mode:v") + 1] == "passthrough"
    # A progressive channel is one picture each, and keeps every caption.
    assert "-filter_complex" not in recorder.convert_args("vaapi", "/x", False)
    assert "-filter_complex" not in recorder.convert_args("software", "/x", True)


def test_the_field_order_is_read_the_way_ffprobe_really_prints_it():
    """A live install printed "tt," twice, and a strict match took it for
    progressive, so captions stayed doubled."""
    assert recorder.field_order_interlaced("tt,\n\ntt,\n")
    assert recorder.field_order_interlaced("bb\n")
    assert not recorder.field_order_interlaced("progressive\n")
    assert not recorder.field_order_interlaced("")


def test_a_channel_that_opens_on_a_progressive_picture_is_still_interlaced():
    """A 1080i channel can open on a film or an advert sent as progressive
    pictures. Any interlaced picture in the first seconds settles it."""
    said = ("field_order=progressive\ninterlaced_frame=0\ninterlaced_frame=0\n"
            "interlaced_frame=1\ninterlaced_frame=1\n")
    assert recorder.probe_says_interlaced(said)
    assert recorder.probe_says_interlaced("field_order=tt\ninterlaced_frame=0\n")
    assert not recorder.probe_says_interlaced(
        "field_order=progressive\n" + "interlaced_frame=0\n" * 180)
    assert not recorder.probe_says_interlaced("")
    # A 1080, 576 or 480 line channel takes the interlaced path even when its
    # first seconds are all progressive: six seconds of film came out combed.
    for h in ("1080", "576", "480"):
        assert recorder.probe_says_interlaced(
            f"field_order=progressive\nheight={h}\n" + "interlaced_frame=0\n" * 180), h
    assert not recorder.probe_says_interlaced(
        "field_order=progressive\nheight=720\n" + "interlaced_frame=0\n" * 180)


def test_every_path_keeps_the_broadcast_timing_and_needs_no_sound():
    """Constant-rate output filled gaps in a broadcast with copies, and each
    copy repeated its captions. A subchannel without sound must not fail."""
    for enc, laced in (("vaapi", True), ("vaapi", False), ("software", True),
                       ("software", False)):
        args = recorder.convert_args(enc, "/x", laced)
        assert args[args.index("-fps_mode:v") + 1] == "passthrough", (enc, laced)
        assert "0:a:0?" in args and "0:a:0" not in args, (enc, laced)


def test_a_broadcast_ffprobe_cannot_read_counts_as_progressive(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "FFPROBE", "/nonexistent/ffprobe")
    (tmp_path / "b.ts").write_bytes(b"x" * 10)
    assert recorder.interlaced(str(tmp_path / "b.ts")) is False


def test_hardware_that_fails_mid_recording_gives_way_to_software(tuner, monkeypatch, caplog):
    monkeypatch.setattr(recorder, "_vaapi", True)
    db.set_setting("recorder_encoder", "auto")
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    job = _until(lambda: recorder._jobs.get(rec_id))
    assert job.converter.enc == "vaapi"
    _until(lambda: job.converter.failed())
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")
    recorder.tick()                                # notices, and starts it again
    assert job.converter.enc == "software"
    assert "hardware encoding failed" in caplog.text
    _until(lambda: recorder.view_info(recorder.get(rec_id)["uid"])["segments"] > 0)
