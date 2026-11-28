"""The recorder with more than one box: where a recording goes, and what it
does when its box stops answering."""
import json
import os
import time

import httpx
import pytest

from app import overtime, recorder, tunerbox
from tests.test_recorder import _airing, _record, _until


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    # A quiet stream is given up on after a second, and a one second hole is
    # worth writing down, so these tests do not wait fifteen seconds.
    monkeypatch.setattr(recorder, "NO_DATA_SECONDS", 1)
    monkeypatch.setattr(recorder, "GAP_MIN_SECONDS", 0.5)


def _ids():
    a, b = tunerbox.rows()
    return a["id"], b["id"]


def _book(vcn, key=None):
    now = int(time.time())
    return recorder.book(_airing("a-" + (key or vcn), "g-" + (key or vcn), vcn, now - 60))


def _go(rec_id):
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))


def _size(rec_id):
    return os.path.getsize(recorder.broadcast_path(recorder.get(rec_id)["uid"]))


def test_a_busy_box_sends_the_recording_to_the_other(two_boxes):
    a, b = two_boxes
    a.plex_holds = ["9.1", "41.1", "9.1", "41.1"]
    rec_id = _book("5.1")
    _go(rec_id)
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["tuner_box"]))
    assert recorder.get(rec_id)["tuner_box"] == str(_ids()[1])
    assert b.streams == ["5.1"] and a.streams == []


def test_a_box_that_stops_mid_recording_hands_over_to_the_other(two_boxes):
    a, b = two_boxes
    rec_id = _book("5.1")
    _go(rec_id)
    assert recorder.get(rec_id)["tuner_box"] == str(_ids()[0])
    a.silent = True
    _until(lambda: b.streams == ["5.1"])
    seen = _size(rec_id)
    _until(lambda: _size(rec_id) > seen)
    rec = _until(lambda: (recorder.tick() or recorder.get(rec_id))["tuner_box"]
                 == str(_ids()[1]) and recorder.get(rec_id))
    gaps = json.loads(rec["gaps"])
    assert len(gaps) == 1 and gaps[0]["seconds"] >= 1


def test_every_box_busy_waits_three_seconds_between_rounds(two_boxes, monkeypatch):
    a, b = two_boxes
    a.plex_holds = ["9.1"] * 4
    b.plex_holds = ["62.1"] * 2
    tries = []
    real = httpx.stream
    monkeypatch.setattr(recorder.httpx, "stream",
                        lambda *x, **k: (tries.append(x[1]), real(*x, **k))[1])
    rec_id = _book("5.1")
    recorder.tick()
    time.sleep(4)
    recorder.tick()
    # Two rounds at most in four seconds, a try on each box in a round.
    assert 2 <= len(tries) <= 4
    assert recorder.get(rec_id)["error"] == "Every tuner is in use."


@pytest.mark.parametrize("how", ["off", "removed"])
def test_turning_off_the_box_mid_recording_moves_it_at_the_next_failure(two_boxes, how):
    a, b = two_boxes
    ida, idb = _ids()
    rec_id = _book("5.1")
    _go(rec_id)
    if how == "off":
        tunerbox.set_enabled(ida, False)
    else:
        assert tunerbox.remove(ida)
    # Nothing breaks while the stream is still good.
    recorder.tick()
    assert recorder.busy(rec_id)
    a.silent = True
    _until(lambda: b.streams == ["5.1"])
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["tuner_box"] == str(idb)))
    assert recorder.busy(rec_id)


def test_a_recording_takes_a_live_tuner_only_on_a_box_that_carries_its_channel(two_boxes):
    a, b = two_boxes
    live = recorder.start_live("9.1")
    _until(lambda: (recorder.tick() or a.live == ["9.1"]))
    a.plex_holds = ["41.1"] * 3
    b.plex_holds = ["5.1"] * 2
    _book("62.1")
    recorder.tick()
    # 9.1 sits on A, which does not carry 62.1: it stays.
    assert recorder.get(live) is not None and "9.1" in a.live
    recorder.shutdown()
    _until(lambda: not a.live)


def test_a_live_tuner_on_a_box_that_carries_the_channel_is_taken(two_boxes):
    a, b = two_boxes
    live = recorder.start_live("62.1")
    _until(lambda: (recorder.tick() or b.live == ["62.1"]))
    b.plex_holds = ["5.1"]
    rec_id = _book("62.1", "x")
    recorder.tick()
    assert recorder.get(live) is None
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))


def test_overtime_wants_the_tuner_only_for_the_waiting_channel(two_boxes):
    a, b = two_boxes
    now = int(time.time())
    game = recorder.get(_book("5.1", "game"))
    b.plex_holds = ["5.1"] * 2
    # Waiting on 9.1: only A carries it, and A is free.
    _book("9.1")
    assert not overtime._tuner_wanted(game, now)
    # Waiting on 62.1: only B carries it, and B is full.
    _book("62.1")
    assert overtime._tuner_wanted(game, now)


def test_a_one_box_install_records_as_before(tuner):
    rec = _record()
    assert rec["state"] == "finished"
    assert rec["tuner_box"] == str(tunerbox.rows()[0]["id"])
    assert json.loads(rec["gaps"]) == []


def test_a_gap_is_said_in_words_on_the_recording(tuner, client):
    rec_id = _book("5.1")
    recorder._set(rec_id, gaps=json.dumps([{"at": 3850, "seconds": 12}]))
    from starlette.requests import Request

    from app.routes import tv
    req = Request({"type": "http", "headers": [(b"host", b"testserver")],
                   "scheme": "http", "server": ("testserver", 80), "path": "/",
                   "query_string": b"", "method": "GET"})
    out = tv.recording_json(req, recorder.get(rec_id))
    assert out["gaps"] == [{"at": 3850, "seconds": 12}]
    assert out["gap_note"] == ["12 seconds missing at 1:04:10, the tuner stopped answering"]


def test_a_dead_box_with_a_stale_lineup_is_not_counted_as_free(two_boxes, monkeypatch):
    a, b = two_boxes
    live = recorder.start_live("9.1")
    _until(lambda: (recorder.tick() or a.live == ["9.1"]))
    a.plex_holds = ["41.1"] * 3                         # A is full: Plex 3, ours 1
    b.status = False                                    # B stops answering
    for box in tunerbox.rows():
        tunerbox._lineups[box["id"]]["tried_at"] = 0
    real = tunerbox._read_lineup

    def b_is_down(box):
        if box["address"] == b.url:
            raise OSError("down")
        return real(box)
    monkeypatch.setattr(tunerbox, "_read_lineup", b_is_down)
    _book("5.1")
    recorder.tick()
    assert recorder.get(live) is None


def test_a_200_with_no_body_is_said_and_the_next_box_is_tried(two_boxes):
    a, b = two_boxes
    ida, idb = _ids()
    a.empty = True
    rec_id = _book("5.1")
    _go(rec_id)
    assert recorder.get(rec_id)["tuner_box"] == str(idb)


def test_a_200_with_no_body_on_the_only_box_says_what_happened(two_boxes):
    a, b = two_boxes
    ida, idb = _ids()
    tunerbox.set_enabled(idb, False)
    a.empty = True
    rec_id = _book("5.1")
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["error"]))
    assert recorder.get(rec_id)["error"] == "The tuner sent nothing for channel 5.1."


def test_overtime_ignores_a_waiting_recording_no_box_carries(two_boxes):
    now = int(time.time())
    game = recorder.get(_book("5.1", "game"))
    _book("99.9")                                  # a channel nobody carries
    assert not overtime._tuner_wanted(game, now)
