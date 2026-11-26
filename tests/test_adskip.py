"""Ad breaks: comskip run on a recording while it records and after, and the
breaks a player is given for its Skip button."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import adskip, db, recorder
from app.web import app
from tests.test_recorder import _airing, _record, _until

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_comskip.py")


@pytest.fixture
def comskip(tmp_path, monkeypatch):
    """The stand-in comskip, and the plan that says what it finds."""
    monkeypatch.setattr(adskip, "COMSKIP", FAKE)
    plan_path = tmp_path / "plan.json"
    stop = tmp_path / "stop"
    plan = {"stop": str(stop), "reached": 0, "live": [], "final": []}

    def set_plan(**kw):
        plan.update(kw)
        plan_path.write_text(json.dumps(plan))

    set_plan()
    monkeypatch.setenv("FAKE_COMSKIP_PLAN", str(plan_path))
    set_plan.stop = stop
    yield set_plan
    stop.write_text("")
    adskip.shutdown()


def _client():
    return TestClient(app)


def test_a_finished_recording_has_its_breaks_found(tuner, comskip):
    comskip(final=[[116.98, 237.1], [720.72, 961.06]])
    rec = _record()
    adskip.tick()
    _until(lambda: (adskip.tick() or recorder.get(rec["id"])["ads_final"] == 1))
    got = _client().get(f"/api/tv/recordings/{rec['id']}/breaks").json()
    assert got["breaks"] == [{"start": 116.98, "end": 237.1},
                             {"start": 720.72, "end": 961.06}]
    assert got["final"] and not got["growing"]


def test_while_recording_only_a_settled_break_is_shown(tuner, comskip):
    """comskip still moves a break near the point it has reached: a break is
    shown once it ended a minute behind that point, and not before."""
    comskip(reached=1000, live=[[116.95, 237.2], [886.0, 961.0]],
            final=[[116.98, 237.1], [720.72, 961.06]])
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    monkey_bytes = adskip.LIVE_START_BYTES
    adskip.LIVE_START_BYTES = 1
    try:
        _until(lambda: (adskip.tick() or db.query(
            "SELECT * FROM ad_breaks WHERE recording_id = ?", (rec_id,))))
    finally:
        adskip.LIVE_START_BYTES = monkey_bytes
    got = _client().get(f"/api/tv/recordings/{rec_id}/breaks").json()
    # 961 is within a minute of 1000: not yet.
    assert got["breaks"] == [{"start": 116.95, "end": 237.2}]
    assert got["growing"] and not got["final"]
    # The recording ends; the live run reads to the end and has the last word.
    recorder.tick(now=recorder.get(rec_id)["stops_at"] + 1)
    comskip.stop.write_text("")
    _until(lambda: (recorder.tick() or adskip.tick()
                    or recorder.get(rec_id)["ads_final"] == 1))
    got = _client().get(f"/api/tv/recordings/{rec_id}/breaks").json()
    assert [b["start"] for b in got["breaks"]] == [116.98, 720.72]
    assert got["final"]


def test_turned_off_means_no_scan_and_no_skip_button(tuner, comskip):
    comskip(final=[[100.0, 200.0]])
    rec = _record()
    db.set_setting("adskip_final", "0")
    for _ in range(5):
        adskip.tick()
    assert recorder.get(rec["id"])["ads_final"] == 0
    # Found while it was on, and kept, but not offered while it is off.
    db.set_setting("adskip_final", "1")
    _until(lambda: (adskip.tick() or recorder.get(rec["id"])["ads_final"] == 1))
    db.set_setting("adskip_final", "0")
    assert _client().get(f"/api/tv/recordings/{rec['id']}/breaks").json()["breaks"] == []
    db.set_setting("adskip_final", "1")
    assert len(_client().get(f"/api/tv/recordings/{rec['id']}/breaks").json()["breaks"]) == 1


def test_a_failing_scan_is_tried_a_few_times_and_then_left(tuner, comskip):
    comskip(fail=True)
    rec = _record()
    now = int(time.time())
    for i in range(adskip.MAX_FAILS + 2):
        at = now + i * (adskip.RETRY_SECONDS + 1)
        adskip.tick(at)
        _until(lambda: all(not s.running() for s in adskip._scans.values()))
        adskip.tick(at)
    assert recorder.get(rec["id"])["ads_fails"] == adskip.MAX_FAILS
    assert recorder.get(rec["id"])["ads_final"] == 0


def test_without_comskip_nothing_runs(tuner):
    rec = _record()
    adskip.tick()
    assert not adskip._scans
    got = _client().get(f"/api/tv/recordings/{rec['id']}/breaks").json()
    assert got["breaks"] == []


def test_the_settings_are_on_unless_turned_off_and_both_forms_save_them():
    assert adskip.live_on() and adskip.final_on()
    c = _client()
    # The web form sends a hidden 0 and, when ticked, a 1 after it.
    r = c.post("/settings/recorder", data={"adskip_live": ["0", "1"], "adskip_final": "0"},
               follow_redirects=False)
    assert r.status_code == 303
    assert db.get_setting("adskip_live") == "1"
    assert db.get_setting("adskip_final") == "0"
    r = c.post("/api/tv/settings", data={"adskip_final": "1"})
    assert r.json()["ok"]
    assert db.get_setting("adskip_final") == "1"
    assert c.get("/api/tv/settings").json()["values"]["adskip_live"] == "1"


def test_a_programme_with_no_ads_is_a_finished_scan_not_a_failure(tuner, comskip):
    """comskip exits 1 when it finds no ads. That is an answer."""
    comskip(final=[])
    rec = _record()
    _until(lambda: (adskip.tick() or recorder.get(rec["id"])["ads_final"] == 1))
    assert recorder.get(rec["id"])["ads_fails"] == 0


def test_a_live_scan_started_again_keeps_the_breaks_already_shown(tuner):
    """A tuner that dropped, or a restart, starts comskip from the beginning.
    The breaks it showed before stay until the new scan has reached them."""
    rec = _record()
    adskip._store_live(rec["id"], [(100.0, 200.0), (900.0, 1000.0)], edge=1100)
    # The new scan has only got to 300 seconds, and settled the first break.
    adskip._store_live(rec["id"], [(100.0, 200.0)], edge=240)
    rows = db.query("SELECT start_s FROM ad_breaks WHERE recording_id = ? ORDER BY start_s",
                    (rec["id"],))
    assert [r["start_s"] for r in rows] == [100.0, 900.0]


def test_a_sports_intro_at_second_zero_is_not_offered_as_an_ad(tuner, comskip):
    """A game opens on its team intro, which comskip reads as an ad from 0.
    That one is not offered; a later break in the game still is."""
    comskip(final=[[0.0, 187.92], [720.72, 961.06]])
    rec = _record()
    with db.tx() as c:
        c.execute("UPDATE recordings SET section = 'sports' WHERE id = ?", (rec["id"],))
    adskip.tick()
    _until(lambda: (adskip.tick() or recorder.get(rec["id"])["ads_final"] == 1))
    got = _client().get(f"/api/tv/recordings/{rec['id']}/breaks").json()
    assert got["breaks"] == [{"start": 720.72, "end": 961.06}]
