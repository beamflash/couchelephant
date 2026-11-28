"""Which box carries a channel, and which one to ask first."""
import time

import pytest

from app import db, tuner, tunerbox
from app import tuner as tuner_mod


def _addr(boxes):
    return [b["address"] for b, _ in boxes]


def test_a_channel_only_one_box_carries_goes_to_that_box(two_boxes):
    a, b = two_boxes
    assert _addr(tuner.pick("62.1")) == [b.url]
    assert _addr(tuner.pick("9.1")) == [a.url]


def test_the_box_with_more_free_tuners_comes_first(two_boxes):
    a, b = two_boxes
    assert _addr(tuner.pick("5.1")) == [a.url, b.url]
    a.plex_holds = ["9.1", "9.1", "41.1"]            # one free on A, two on B
    assert _addr(tuner.pick("5.1")) == [b.url, a.url]


def test_the_url_is_the_lineups_own(two_boxes):
    a, _ = two_boxes
    assert tuner.pick("9.1")[0][1] == f"{a.url}/auto/v9.1"


def test_a_box_without_status_counts_as_unknown_and_sorts_after(two_boxes):
    a, b = two_boxes
    b.status = False
    assert _addr(tuner.pick("5.1")) == [a.url, b.url]
    assert tuner.free("62.1") == 2
    # Even with fewer tuners than ours, an answered box still sorts first.
    a.total = 1
    assert _addr(tuner.pick("5.1")) == [a.url, b.url]


def test_a_box_whose_status_hangs_does_not_slow_the_pick(two_boxes):
    a, b = two_boxes
    b.status_delay = 6
    t = time.time()
    got = tuner.pick("5.1")
    assert time.time() - t < 3
    assert _addr(got) == [a.url, b.url]


def test_a_stale_box_is_tried_last(two_boxes, monkeypatch):
    a, b = two_boxes
    tuner.pick("5.1")                                  # both lineups read
    for box in tunerbox.rows():
        tunerbox._lineups[box["id"]]["tried_at"] = 0   # both are due for a new read
    real = tunerbox._read_lineup

    def fails_for_a(box):
        if box["address"] == a.url:
            raise OSError("down")
        return real(box)
    monkeypatch.setattr(tunerbox, "_read_lineup", fails_for_a)
    b.total = 1                                        # A has more free tuners, still last
    assert _addr(tuner.pick("5.1")) == [b.url, a.url]


def test_one_box_with_no_lineup_gets_the_hand_built_url(tuner, monkeypatch):
    def no_lineup(box):
        raise OSError("no lineup")
    monkeypatch.setattr(tunerbox, "_read_lineup", no_lineup)
    box = tunerbox.rows()[0]
    got = tuner_mod.pick("5.1")
    assert len(got) == 1
    host = box["address"].split("://", 1)[1].split(":")[0]
    assert got[0][1] == f"http://{host}:5004/auto/v5.1"


def test_a_refused_channel_is_refused_in_words(two_boxes):
    a, b = two_boxes
    a.extra = [{"GuideNumber": "105.1", "DRM": 1, "URL": "x"}]
    a.lineup = [("5.1", "KCTV")]
    with pytest.raises(tuner.TunerError, match="encrypted"):
        tuner.check("105.1")


def test_a_station_one_box_refuses_goes_to_the_other(two_boxes):
    a, b = two_boxes
    a.lineup = []
    a.extra = [{"GuideNumber": "5.1", "AudioCodec": "AC4", "URL": f"{a.url}/auto/v5.1"}]
    assert _addr(tuner.pick("5.1")) == [b.url]
    assert tuner.refused_channels() == set()


def test_a_channel_every_box_refuses_is_listed_as_refused(two_boxes):
    a, b = two_boxes
    a.lineup, b.lineup = [], []
    row = {"GuideNumber": "109.1", "AudioCodec": "AC4", "URL": "x"}
    a.extra, b.extra = [row], [row]
    assert tuner.refused_channels() == {"109.1"}
    with pytest.raises(tuner.TunerError, match="ATSC 3.0"):
        tuner.check("109.1")


def test_a_channel_nobody_carries_says_so(two_boxes):
    with pytest.raises(tuner.TunerError, match="No tuner carries channel 77.7"):
        tuner.pick("77.7")


def test_no_boxes_says_so_plainly(clean_db):
    with pytest.raises(tuner.TunerError, match="No tuner address"):
        tuner.pick("5.1")


def test_free_for_a_channel_counts_only_boxes_that_carry_it(two_boxes):
    assert tuner.free("62.1") == 2
    assert tuner.free("9.1") == 4
    assert tuner.free() == 6


def test_total_adds_every_box(two_boxes):
    assert tuner.total() == 6


def test_total_skips_a_box_not_seen_for_an_hour(two_boxes):
    a, b = two_boxes
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET last_seen = ? WHERE address = ?",
                  (int(time.time()) - 7200, b.url))
    assert tuner.total() == 4


def test_a_turned_off_box_is_never_picked(two_boxes):
    a, b = two_boxes
    for box in tunerbox.rows():
        if box["address"] == a.url:
            tunerbox.set_enabled(box["id"], False)
    assert _addr(tuner.pick("5.1")) == [b.url]
    assert tuner.free() == 2
    with pytest.raises(tuner.TunerError):
        tuner.pick("9.1")


def test_a_box_with_no_status_counts_what_we_hold_on_it(two_boxes):
    a, b = two_boxes
    b.status = False
    box_b = [x for x in tunerbox.rows() if x["address"] == b.url][0]
    tuner.hold(1, box_b["id"], "62.1")
    assert tuner.free("62.1") == 1
    tuner.release(1)
    assert tuner.free("62.1") == 2


def test_each_tuner_says_what_holds_it(two_boxes):
    a, _ = two_boxes
    box_a = [x for x in tunerbox.rows() if x["address"] == a.url][0]
    a.plex_holds = ["9.1"]
    a.live = ["5.1"]
    tuner.hold(7, box_a["id"], "5.1")
    tuner.describe_hold = lambda job: "Recording: Chiefs at Bills"
    try:
        got = tuner.box_tuners(box_a)
    finally:
        tuner.describe_hold = None
        tuner.release(7)
    states = sorted(t["state"] for t in got)
    assert states == ["free", "free", "other", "ours"]
    other = [t for t in got if t["state"] == "other"][0]
    assert other["label"] == "In use by another app"
    assert [t["label"] for t in got if t["state"] == "ours"] == ["Recording: Chiefs at Bills"]


def test_without_a_describer_a_live_hold_reads_live(two_boxes):
    a, _ = two_boxes
    box_a = [x for x in tunerbox.rows() if x["address"] == a.url][0]
    a.live = ["5.1"]
    tuner.hold(8, box_a["id"], "5.1")
    try:
        got = tuner.box_tuners(box_a)
    finally:
        tuner.release(8)
    assert "Live: 5.1" in [t["label"] for t in got]
