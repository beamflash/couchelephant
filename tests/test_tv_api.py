"""The choice of recorder in the web app's own API, and what a television
client reads: the lists, the video, and how it is let in."""
import time

from app import auth, db, recorder
from tests import fake_plex
from tests.test_recorder import _airing, _record, _until


def _live_airing():
    return db.one("SELECT id FROM airings WHERE premiere = 1 AND drm = 0")["id"]


# ---------- "Recorded by", in the booking ----------

def test_the_choice_is_offered_with_plex_as_the_default(client, synced, tuner):
    d = client.get("/api/record/options", params={"airing_id": _live_airing()}).json()
    assert d["recorder"] == {"available": True, "default": "plex"}


def test_the_default_setting_decides_what_a_plain_booking_does(client, synced, tuner):
    db.set_setting("recorder_default", "couchelephant")
    aid = _live_airing()
    d = client.get("/api/record/options", params={"airing_id": aid}).json()
    assert d["recorder"]["default"] == "couchelephant"
    r = client.post("/api/record", data={"airing_id": aid, "settings": "{}"}).json()
    assert r["recorder"] == "couchelephant"
    assert fake_plex.STATE.created == []


def test_recording_one_broadcast_with_couchelephant_and_cancelling_it(client, synced, tuner):
    aid = _live_airing()
    r = client.post("/api/record", data={"airing_id": aid, "settings": "{}",
                                         "recorder": "couchelephant"})
    assert r.json()["ok"] is True, r.json()
    assert db.one("SELECT state FROM recordings WHERE airing_id = ?", (aid,))["state"] \
        == "scheduled"
    assert fake_plex.STATE.created == [], "Plex was not asked to record it too"
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (aid,)) is None

    r = client.post("/api/record/cancel", data={"airing_id": aid})
    assert r.json()["ok"] is True
    assert db.one("SELECT 1 FROM recordings WHERE airing_id = ?", (aid,)) is None


def test_a_broadcast_plex_already_has_is_not_recorded_twice(client, synced, tuner):
    aid = _live_airing()
    assert client.post("/api/record", data={"airing_id": aid, "settings": "{}"}).json()["ok"]
    r = client.post("/api/record", data={"airing_id": aid, "settings": "{}",
                                         "recorder": "couchelephant"})
    assert r.status_code == 409
    assert "already set to record" in r.json()["error"]
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 0


def test_asking_for_our_recorder_where_it_cannot_run_says_why(client, synced, tuner,
                                                             monkeypatch):
    monkeypatch.setattr(recorder, "REC_DIR", "/nonexistent-ce-test")
    aid = _live_airing()
    d = client.get("/api/record/options", params={"airing_id": aid}).json()
    assert d["recorder"] == {"available": False, "default": "plex"}
    r = client.post("/api/record", data={"airing_id": aid, "settings": "{}",
                                         "recorder": "couchelephant"})
    assert r.status_code == 400
    assert "Mount one on the container" in r.json()["error"]


def test_a_pass_can_be_switched_to_our_recorder_and_says_so(client, synced, tuner):
    r = client.post("/api/rules", data={"kind": "series", "series": "Jeopardy!",
                                        "recorder": "couchelephant"})
    assert r.json()["ok"] is True, r.json()
    assert r.json()["ce_rule"] is True, "Plex cannot hold this rule"
    rule = [x for x in client.get("/api/rules").json()["rules"] if x["who"] == "ce"][0]
    assert rule["recorder"] == "couchelephant"
    assert fake_plex.STATE.created == []

    client.post(f"/api/rules/{rule['id']}", data={"recorder": "plex"})
    assert db.one("SELECT recorder FROM passes")["recorder"] == "plex"
    # Not sent means unchanged.
    client.post(f"/api/rules/{rule['id']}", data={"enabled": "1"})
    assert db.one("SELECT recorder FROM passes")["recorder"] == "plex"


# ---------- what a television reads ----------

def test_home_lists_what_is_recording_with_a_playlist_that_plays(client, tuner):
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    uid = recorder.get(rec_id)["uid"]
    _until(lambda: recorder.view_info(uid)["segments"] > 0)

    home = client.get("/api/tv/home").json()
    card = home["live_now"][0]
    assert card["recorded_by"] == "couchelephant"
    assert (card["title"], card["subtitle"]) == ("Chiefs at Broncos", "NFL Football")
    assert card["playable"] is True and card["complete"] is False

    path = card["playlist"].replace("http://testserver", "")
    pl = client.get(path)
    assert pl.status_code == 200
    assert pl.headers["cache-control"] == "no-store", "it changes while it records"
    assert "seg00000.ts" in pl.text and "#EXT-X-ENDLIST" not in pl.text
    assert client.get(path.replace("index.m3u8", "seg00000.ts")).status_code == 200
    # Asked to start at the beginning, the playlist says so itself. Apple's
    # player opens a growing playlist at its live end otherwise.
    assert "#EXT-X-START" not in pl.text
    first = client.get(path + "?start=0").text.splitlines()
    assert first[:2] == ["#EXTM3U", "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES"]


def test_a_plex_recording_is_listed_and_never_playable(client, tuner):
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, status, title, parent_title, channel_vcn, "
                  "begins_at, ends_at) VALUES ('g9', 'inprogress', 'Raiders at Chargers', "
                  "'NFL Football', '9.1', ?, ?)", (now - 600, now + 3000))
    card = client.get("/api/tv/home").json()["live_now"][0]
    assert card["recorded_by"] == "plex"
    assert card["playable"] is False
    assert card["note"] == "Cannot play while Plex is recording"


def test_the_video_paths_serve_nothing_but_the_viewing_copy(client, tuner):
    rec = _record()
    assert client.get(f"/hls/{rec['uid']}/index.m3u8").status_code == 200
    assert client.get(f"/hls/{rec['uid']}/broadcast.ts").status_code == 404
    assert client.get(f"/hls/{rec['uid']}/ffmpeg.log").status_code == 404
    assert client.get("/hls/../index.m3u8").status_code == 404
    assert client.get("/hls/" + "z" * 32 + "/index.m3u8").status_code == 404


def test_a_position_is_kept_and_counts_as_watching(client, tuner):
    rec = _record()
    r = client.post(f"/api/tv/recordings/{rec['id']}/position", data={"seconds": "754.5"})
    assert r.json()["ok"] is True
    got = client.get(f"/api/tv/recordings/{rec['id']}").json()["recording"]
    assert got["position"] == 754.5
    assert recorder.watched_recently(recorder.get(rec["id"]))
    assert rec["id"] in [x["id"] for x in client.get("/api/tv/home").json()["recent"]]


def test_live_viewing_starts_a_session_and_keepalive_holds_it(client, tuner):
    r = client.post("/api/tv/live/9.1").json()
    assert r["ok"] is True
    rec_id = r["recording"]["id"]
    _until(lambda: tuner.live == ["9.1"])
    assert client.post(f"/api/tv/live/{rec_id}/keepalive").json()["ok"] is True
    # A live session is not a recording, and is not listed as one.
    home = client.get("/api/tv/home").json()
    assert home["live_now"] == [] and home["recent"] == []


def test_live_viewing_of_a_channel_we_cannot_decode_is_refused(client, tuner):
    tuner.extra = [{"GuideNumber": "109.1", "AudioCodec": "AC4", "URL": "x"}]
    r = client.post("/api/tv/live/109.1")
    assert r.status_code == 400 and "ATSC 3.0" in r.json()["error"]


# ---------- letting a television in ----------

def test_with_sign_in_off_a_television_needs_no_pairing(client, tuner):
    hello = client.get("/api/tv/hello").json()
    assert hello["pairing_required"] is False and hello["paired"] is True
    assert client.get("/api/tv/home").status_code == 200


def test_with_sign_in_on_a_television_pairs_by_code(client, tuner):
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    assert client.get("/api/tv/home").status_code == 401
    assert client.get("/api/tv/hello").json()["pairing_required"] is True

    asked = client.post("/api/tv/pair", data={"name": "Living room"}).json()
    assert len(asked["code"]) == 6
    assert client.post("/api/tv/pair/poll",
                       data={"secret": asked["secret"]}).json()["approved"] is False
    # The television cannot approve itself.
    assert client.post("/api/tv/devices/approve",
                       data={"code": asked["code"]}).status_code == 401

    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    assert client.post("/api/tv/devices/approve",
                       data={"code": asked["code"].lower()}).json()["name"] == "Living room"
    client.cookies.clear()

    got = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()
    key = got["key"]
    # Handed over once. The secret is spent.
    assert client.post("/api/tv/pair/poll",
                       data={"secret": asked["secret"]}).status_code == 404

    tv = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/tv/home", headers=tv).status_code == 200
    assert client.get("/api/tv/hello", headers=tv).json()["paired"] is True
    # The key opens the television's own pages and nothing else.
    assert client.get("/api/schedule", headers=tv).status_code == 401
    assert client.get("/api/tv/devices", headers=tv).status_code == 401
    assert key not in str(db.query("SELECT * FROM tv_devices")[0]["key_hash"])


def test_a_viewer_account_cannot_approve_a_television(client, tuner):
    db.set_setting("auth_mode", "local")
    auth.create_user("owner", "a-good-password")
    kid = auth.create_user("kid", "a-good-password", role="user")
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(kid))
    assert client.post("/api/tv/devices/approve",
                       data={"code": asked["code"]}).status_code == 403


def test_a_removed_television_is_locked_out(client, tuner):
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    client.post("/api/tv/devices/approve", data={"code": asked["code"]})
    dev = client.get("/api/tv/devices").json()["devices"][0]["id"]
    client.cookies.clear()
    key = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    tv = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/tv/home", headers=tv).status_code == 200
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    client.post(f"/api/tv/devices/{dev}/delete")
    client.cookies.clear()
    assert client.get("/api/tv/home", headers=tv).status_code == 401
