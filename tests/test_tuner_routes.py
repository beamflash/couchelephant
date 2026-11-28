"""The tuner API and what Settings reads: every box, every tuner's line."""
import pytest

from app import auth, db, reportbuild, tuner, tunerbox
from tests import fake_tuner


@pytest.fixture
def lines(two_boxes, monkeypatch):
    """Box A: one tuner is ours (a recording), one is Plex's. Box B is idle."""
    a, b = two_boxes
    box_a = tunerbox.rows()[0]
    a.live = ["5.1"]
    a.plex_holds = ["9.1"]
    tuner.hold(7, box_a["id"], "5.1")
    monkeypatch.setattr(tuner, "describe_hold", lambda job: "Recording: Chiefs at Bills")
    yield a, b
    tuner.release(7)


def test_the_list_says_what_every_tuner_is_doing(client, lines):
    got = client.get("/api/tv/tuners").json()
    assert got["ok"] and len(got["boxes"]) == 2
    a, b = got["boxes"]
    assert a["found_by"] == "typed" and a["enabled"] and a["address"].startswith("http://")
    assert {"id", "name", "model", "address", "found_by", "enabled", "error",
            "stale", "tuners"} <= set(a)
    labels = [t["label"] for t in a["tuners"]]
    assert labels.count("Recording: Chiefs at Bills") == 1
    assert labels.count("In use by another app") == 1
    assert labels.count("Free") == 2
    assert [t["state"] for t in b["tuners"]] == ["free", "free"]
    assert got["free"] == 4 and got["total"] == 6
    assert got["can_find_on_network"] is True


def test_the_bridge_note_flag_follows_discovery(client, two_boxes):
    db.set_setting("tuner_broadcast_ok", "0")
    assert client.get("/api/tv/tuners").json()["can_find_on_network"] is False


def test_add_turn_off_and_remove_a_typed_box(client, two_boxes):
    url, stop, _ = fake_tuner.start_box(device_id="FAKE000C", total=2)
    try:
        r = client.post("/api/tv/tuners", data={"address": url})
        assert r.status_code == 200 and r.json()["box"]["address"] == url
        box_id = r.json()["box"]["id"]
        assert len(client.get("/api/tv/tuners").json()["boxes"]) == 3
        assert client.post(f"/api/tv/tuners/{box_id}", data={"enabled": "0"}).json()["ok"]
        listed = [b for b in client.get("/api/tv/tuners").json()["boxes"] if b["id"] == box_id]
        assert listed[0]["enabled"] is False and listed[0]["tuners"] == []
        assert client.delete(f"/api/tv/tuners/{box_id}").json()["ok"]
        assert len(client.get("/api/tv/tuners").json()["boxes"]) == 2
    finally:
        stop()


def test_a_bad_address_is_refused_in_words(client, two_boxes):
    r = client.post("/api/tv/tuners", data={"address": "  "})
    assert r.status_code == 400 and r.json()["ok"] is False and r.json()["error"]


def test_a_found_box_cannot_be_removed(client, two_boxes):
    box = tunerbox.rows()[0]
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET found_by = 'plex' WHERE id = ?", (box["id"],))
    r = client.delete(f"/api/tv/tuners/{box['id']}")
    assert r.status_code == 400 and r.json()["ok"] is False
    assert tunerbox.get(box["id"])


def test_an_old_app_posting_tuner_url_adds_a_typed_box(client):
    url, stop, _ = fake_tuner.start_box(device_id="FAKE000D", total=2)
    try:
        client.post("/settings/recorder", data={"tuner_url": url})
        rows = tunerbox.rows()
        assert [r["address"] for r in rows] == [url] and rows[0]["found_by"] == "typed"
    finally:
        stop()


def test_recorder_status_keeps_the_old_shape_and_adds_boxes(client, two_boxes):
    t = client.get("/api/recorder/status").json()["tuner"]
    assert t["url"] and t["typed"] is True and t["total"] == 6 and t["free"] == 6
    assert len(t["boxes"]) == 2


def test_a_viewer_cannot_change_tuners(client, two_boxes):
    db.set_setting("auth_mode", "local")
    auth.create_user("owner", "a-good-password")
    viewer = auth.create_user("kid", "a-good-password", role="user")
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(viewer))
    assert client.get("/api/tv/tuners").status_code == 403
    assert client.post("/api/tv/tuners", data={"address": "192.0.2.9"}).status_code == 403
    assert client.delete("/api/tv/tuners/1").status_code == 403


def test_the_report_carries_models_and_never_addresses(client, two_boxes):
    p = reportbuild.server_part(reportbuild.Scrubber())
    t = p["tuner"]
    assert t["count"] == 2 and t["boxes"][0]["found_by"] == "typed"
    assert t["boxes"][0]["model"] == "HDFX-4K"
    text = str(p["tuner"])
    for b in tunerbox.rows():
        assert b["address"] not in text and "127.0.0.1" not in text
