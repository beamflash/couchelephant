"""The choice of recorder in the web app's own API, and what a television
client reads: the lists, the video, and how it is let in."""
import hmac
import os
import time

from app import auth, db, recorder
from app.routes import tv as tv_routes
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

    master = card["playlist"].replace("http://testserver", "")
    assert master.endswith("/master.m3u8")
    # The master playlist names the captions, or Apple's player never offers them.
    m = client.get(master)
    assert m.status_code == 200 and m.headers["cache-control"] == "no-store"
    assert 'INSTREAM-ID="CC1"' in m.text and 'CLOSED-CAPTIONS="cc"' in m.text
    assert "LANGUAGE=" not in m.text, "CC1 is in whatever language the station uses"
    assert m.text.rstrip().splitlines()[-1] == "index.m3u8"
    # "From the beginning" is carried through to the playlist it points at.
    assert client.get(master + "?start=0").text.rstrip().splitlines()[-1] \
        == "index.m3u8?start=0"

    path = master.replace("master.m3u8", "index.m3u8")
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
    assert client.get("/hls/" + "z" * 32 + "/master.m3u8").status_code == 404


def test_what_is_on_now_follows_a_live_channel_into_the_next_programme(client, tuner):
    now = int(time.time())
    with db.tx() as c:
        for guid, title, summary, begins in (("p1", "First Show", "One.", now - 600),
                                             ("p2", "Second Show", "Two.", now + 600)):
            c.execute("INSERT INTO programs (guid, title, summary, type) "
                      "VALUES (?, ?, ?, 'episode')", (guid, title, summary))
            c.execute("INSERT INTO airings (id, program_guid, channel_vcn, channel_title, "
                      "begins_at, ends_at) VALUES (?, ?, '7.1', 'KTKA', ?, ?)",
                      ("air-" + guid, guid, begins, begins + 1200))
        cur = c.execute("INSERT INTO recordings (uid, kind, state, channel_vcn, "
                        "channel_title, started_at) VALUES (?, 'live', 'recording', "
                        "'7.1', 'KTKA', ?)", ("a" * 32, now))
        rec_id = cur.lastrowid
    d = client.get(f"/api/tv/recordings/{rec_id}/now", params={"seconds": 0}).json()
    assert (d["program"]["title"], d["program"]["summary"]) == ("First Show", "One.")
    assert d["program"]["channel"]["number"] == "7.1"
    later = client.get(f"/api/tv/recordings/{rec_id}/now", params={"seconds": 900}).json()
    assert later["program"]["title"] == "Second Show"


def test_a_live_channel_card_shows_the_picture_of_what_is_on(client, tuner):
    from app.routes import tv
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO programs (guid, title, art, type) VALUES "
                  "('pa', 'Show', 'https://example.test/art.jpg', 'episode')")
        c.execute("INSERT INTO airings (id, program_guid, channel_vcn, begins_at, ends_at) "
                  "VALUES ('air-pa', 'pa', '8.1', ?, ?)", (now - 60, now + 600))
    assert tv._art_on_now("8.1") == "https://example.test/art.jpg"
    assert tv._art_on_now("99.1") == ""


def test_a_recording_the_guide_forgot_keeps_its_own_description(client, tuner):
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute("INSERT INTO recordings (uid, kind, state, title, show_title, "
                        "summary, channel_vcn, channel_title, begins_at, ends_at, "
                        "started_at) VALUES (?, 'recording', 'finished', 'Pilot', "
                        "'The Show', 'Kept at booking.', '9.1', 'KMBC', ?, ?, ?)",
                        ("b" * 32, now - 9000, now - 7200, now - 9000))
        rec_id = cur.lastrowid
    d = client.get(f"/api/tv/recordings/{rec_id}/now", params={"seconds": 60}).json()
    assert d["program"]["title"] == "The Show"
    assert d["program"]["summary"] == "Kept at booking."
    assert client.get("/api/tv/recordings/999999/now").status_code == 404


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
    assert r["recording"]["pause_limit"] == 30 * 60, "the players are told the limit"
    # A live session is not a recording, and is not listed as one.
    home = client.get("/api/tv/home").json()
    assert home["live_now"] == [] and home["recent"] == []
    assert client.post(f"/api/tv/live/{rec_id}/stop").json()["ok"] is True
    assert recorder.get(rec_id) is None


def test_live_tv_left_while_playing_gives_the_tuner_back_in_a_minute(client, tuner):
    """An app closed or put away while live TV plays sends nothing more. Only
    a pause the person made holds the tuner for the pause limit."""
    import time
    playing = client.post("/api/tv/live/9.1").json()["recording"]["id"]
    paused = client.post("/api/tv/live/5.1").json()["recording"]["id"]
    _until(lambda: len(tuner.live) == 2)
    assert client.post(f"/api/tv/live/{playing}/keepalive", data={"paused": "0"}).json()["ok"]
    assert client.post(f"/api/tv/live/{paused}/keepalive", data={"paused": "1"}).json()["ok"]
    recorder.tick(now=int(time.time()) + recorder.LIVE_IDLE_SECONDS + 5)
    assert recorder.get(playing) is None, "the playing one was left: its tuner goes back"
    assert recorder.get(paused) is not None, "the paused one is held for the pause limit"
    _until(lambda: len(tuner.live) == 1)


def test_live_viewing_of_a_channel_we_cannot_decode_is_refused(client, tuner):
    tuner.extra = [{"GuideNumber": "109.1", "AudioCodec": "AC4", "URL": "x"}]
    r = client.post("/api/tv/live/109.1")
    assert r.status_code == 400 and "ATSC 3.0" in r.json()["error"]


# ---------- letting a television in ----------

def test_with_sign_in_off_a_television_needs_no_pairing(client, tuner):
    hello = client.get("/api/tv/hello").json()
    assert hello["pairing_required"] is False
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


# ---------- the guide, recording and deleting from a television ----------

def _pair(client, allow_delete=False):
    """A television let in, the way the app does it. Answers its headers."""
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    key = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    if allow_delete:
        dev = db.one("SELECT id FROM tv_devices ORDER BY id DESC")["id"]
        client.post(f"/api/tv/devices/{dev}/settings", data={"allow_delete": "1"})
    return {"Authorization": f"Bearer {key}"}


def test_with_sign_in_off_a_television_still_gets_a_key_of_its_own(client, tuner):
    """Its settings hang off it, so it registers even when nothing guards the door."""
    assert client.get("/api/tv/hello").json()["paired"] is False
    tv = _pair(client)
    hello = client.get("/api/tv/hello", headers=tv).json()
    assert hello["paired"] is True and hello["can_delete"] is False
    assert [d["name"] for d in client.get("/api/tv/devices").json()["devices"]] == ["Den"]


def test_the_guide_lists_channels_with_what_is_on_from_now(client, tuner):
    now = int(time.time())
    _airing("a-now", "g-now", "5.1", now - 600, title="On Now")
    _airing("a-next", "g-next", "5.1", now + 3000, title="On Next")
    _airing("a-old", "g-old", "5.1", now - 9000, title="Long Over")
    recorder.book(_airing("a-next", "g-next", "5.1", now + 3000, title="On Next"))
    g = client.get("/api/tv/guide").json()
    row = [c for c in g["channels"] if c["channel"]["number"] == "5.1"][0]
    assert [p["title"] for p in row["programs"]] == ["On Now", "On Next"]
    assert [p["recording"] for p in row["programs"]] == [None, "couchelephant"]
    assert row["watchable"] is True


def test_a_programme_says_what_recording_every_one_would_mean(client, tuner):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 600)
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name) VALUES (236, 'Chiefs')")
    p = client.get("/api/tv/program", params={"airing_id": "a1"}).json()["program"]
    assert p["teams"] == [{"id": 236, "name": "Chiefs", "followed": False}]
    assert p["series"] == "", "a game is followed by its team, not by 'NFL Football'"
    assert p["recordable"] is True and p["recording"] is None


def test_a_television_records_one_broadcast_and_cancels_it(client, tuner, plex):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 600)
    tv = _pair(client)
    r = client.post("/api/tv/record", data={"airing_id": "a1"}, headers=tv).json()
    assert r["ok"] is True and r["recorder"] == "couchelephant"
    up = client.get("/api/tv/recordings", headers=tv).json()["upcoming"]
    assert [(u["title"], u["booked_by"], u["recorder"]) for u in up] == \
        [("Chiefs at Broncos", "One recording", "couchelephant")]
    assert client.post("/api/tv/record/cancel", data={"airing_id": "a1"},
                       headers=tv).json()["ok"] is True
    assert client.get("/api/tv/recordings", headers=tv).json()["upcoming"] == []


def test_a_television_follows_a_team_and_the_list_names_the_pass(client, tuner, plex):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 600)
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name) VALUES (236, 'Chiefs')")
    r = client.post("/api/tv/record", data={"airing_id": "a1", "scope": "team",
                                            "team_id": "236"}).json()
    assert r["ok"] is True, r
    up = client.get("/api/tv/recordings").json()["upcoming"]
    assert [(u["booked_by"], u["booked_kind"], u["recorder"]) for u in up] == \
        [("Chiefs pass", "pass", "couchelephant")]
    assert fake_plex.STATE.created == []


def test_following_from_a_television_keeps_an_existing_pass_and_its_limits(client, tuner, plex):
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name) VALUES (236, 'Chiefs')")
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, networks, enabled, "
                  "created_at) VALUES ('team', 236, 'Chiefs', 'u1', ?, 1, 0)", ('["CBS"]',))
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 600)
    client.post("/api/tv/record", data={"airing_id": "a1", "scope": "team", "team_id": "236"})
    p = db.one("SELECT * FROM passes")
    assert (p["recorder"], p["networks"]) == ("couchelephant", '["CBS"]')
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 1


def test_recorded_is_sorted_by_programme_and_says_what_kind_each_is(client, tuner):
    _record("5.1", title="Zebra Hour", show="Zebra Hour")
    with db.tx() as c:
        c.execute("UPDATE programs SET section = 'sports' WHERE guid = 'g-9.1'")
    b = _record("9.1", title="Apples at Pears", show="Autumn Football")
    recs = client.get("/api/tv/recordings").json()["recorded"]
    assert [r["group"] for r in recs] == ["Autumn Football", "Zebra Hour"]
    assert recs[0]["id"] == b["id"]


def test_a_television_cannot_delete_until_its_own_setting_allows_it(client, tuner):
    rec = _record()
    tv = _pair(client)
    r = client.post(f"/api/tv/recordings/{rec['id']}/delete", headers=tv)
    assert r.status_code == 403 and "not allowed to delete" in r.json()["error"]
    assert recorder.get(rec["id"]) is not None
    dev = db.one("SELECT id FROM tv_devices")["id"]
    client.post(f"/api/tv/devices/{dev}/settings", data={"allow_delete": "1"})
    assert client.get("/api/tv/home", headers=tv).json()["can_delete"] is True
    assert client.post(f"/api/tv/recordings/{rec['id']}/delete", headers=tv).json()["ok"]
    assert recorder.get(rec["id"]) is None
    assert not os.path.exists(recorder.rec_dir(rec["uid"]))


def test_deleting_a_recording_plex_has_removes_it_from_plex_too(client, tuner, plex):
    from app import handover
    for k, v in (("recorder_shows_folder", "tv"), ("recorder_shows_section", "2")):
        db.set_setting(k, v)
    os.makedirs(os.path.join(handover.LIB_DIR, "tv"))
    rec = _record()
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex" and os.path.exists(done["plex_path"])
    tv = _pair(client, allow_delete=True)
    assert client.post(f"/api/tv/recordings/{rec['id']}/delete", headers=tv).json()["ok"]
    assert fake_plex.STATE.library_deleted == [done["plex_rating_key"]]
    assert not os.path.exists(done["plex_path"]), "the file goes, not only the entry"
    assert recorder.get(rec["id"]) is None


def test_nothing_is_deleted_when_plex_cannot_be_reached(client, tuner, plex):
    from app import handover
    for k, v in (("recorder_shows_folder", "tv"), ("recorder_shows_section", "2")):
        db.set_setting(k, v)
    os.makedirs(os.path.join(handover.LIB_DIR, "tv"))
    rec = _record()
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    db.set_setting("plex_url", "http://127.0.0.1:1")
    r = client.post(f"/api/tv/recordings/{rec['id']}/delete")
    assert r.status_code == 409 and "nothing was deleted" in r.json()["error"]
    assert os.path.exists(done["plex_path"]) and recorder.get(rec["id"]) is not None


def test_a_series_leads_with_its_name_and_sport_with_the_game(client, tuner):
    now = int(time.time())
    _airing("a-game", "g-game", "5.1", now - 600)             # sport, as _airing makes it
    _airing("a-ep", "g-ep", "9.1", now - 600, title="Eve of Destruction",
            show="Just Shoot Me")
    with db.tx() as c:
        c.execute("UPDATE programs SET section = 'tv' WHERE guid = 'g-ep'")
    rows = {c["channel"]["number"]: c["programs"][0]
            for c in client.get("/api/tv/guide").json()["channels"]}
    assert (rows["5.1"]["title"], rows["5.1"]["subtitle"]) == ("Chiefs at Broncos", "NFL Football")
    assert (rows["9.1"]["title"], rows["9.1"]["subtitle"]) == ("Just Shoot Me", "Eve of Destruction")


# ---------- which recorder a new recording starts with ----------

def test_the_recorder_is_the_default_wherever_it_is_on(client, synced, tuner, monkeypatch):
    db.set_setting("recorder_default", "auto")
    aid = _live_airing()
    assert client.get("/api/record/options",
                      params={"airing_id": aid}).json()["recorder"]["default"] == "couchelephant"
    monkeypatch.setattr(recorder, "REC_DIR", "/nonexistent-ce-test")
    assert client.get("/api/record/options",
                      params={"airing_id": aid}).json()["recorder"]["default"] == "plex"


def test_an_install_seeded_with_plex_moves_to_auto_once_and_a_choice_sticks(clean_db):
    with db.tx() as c:
        c.execute("DELETE FROM settings WHERE key = 'recorder_default_auto'")
    db.set_setting("recorder_default", "plex")        # as 1.5.5 seeded it
    db.init()
    assert db.get_setting("recorder_default") == "auto"
    db.set_setting("recorder_default", "plex")        # now somebody chose it
    db.init()
    assert db.get_setting("recorder_default") == "plex"


# ---------- passes, from a television ----------

def _chiefs_pass(client):
    now = int(time.time())
    _airing("a1", "g1", "5.1", now + 600)
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name) VALUES (236, 'Chiefs')")
    client.post("/api/tv/record", data={"airing_id": "a1", "scope": "team", "team_id": "236"})
    return db.one("SELECT id FROM passes")["id"]


def test_the_passes_are_listed_by_the_tab_they_belong_in(client, tuner, plex):
    _chiefs_pass(client)
    client.post("/api/tv/record", data={"airing_id": "a1", "scope": "series"})
    got = client.get("/api/tv/recordings").json()
    tabs = {p["title"]: (p["tab"], p["recorder"], p["editable"]) for p in got["passes"]}
    assert tabs == {"Chiefs": ("sports", "couchelephant", True),
                    "NFL Football": ("season", "couchelephant", True)}
    assert got["recorder_available"] is True


def test_a_television_changes_one_thing_on_a_pass_and_nothing_else(client, tuner, plex):
    pid = _chiefs_pass(client)
    with db.tx() as c:
        c.execute("UPDATE passes SET networks = ?", ('["CBS"]',))
    r = client.post(f"/api/tv/passes/{pid}", data={"end_minutes": "30"}).json()
    assert r["ok"] is True
    p = db.one("SELECT * FROM passes")
    assert (p["recorder"], p["networks"], p["enabled"]) == ("couchelephant", '["CBS"]', 1)
    assert db.unjs(p["prefs"])["endOffsetMinutes"] == "30"
    client.post(f"/api/tv/passes/{pid}", data={"recorder": "plex", "enabled": "0"})
    p = db.one("SELECT * FROM passes")
    assert (p["recorder"], p["enabled"], p["networks"]) == ("plex", 0, '["CBS"]')
    listed = client.get("/api/tv/recordings").json()["passes"][0]
    assert (listed["end_minutes"], listed["enabled"], listed["networks"]) == (30, False, ["CBS"])


def test_padding_a_television_did_not_offer_is_not_stored(client, tuner, plex):
    pid = _chiefs_pass(client)
    client.post(f"/api/tv/passes/{pid}", data={"end_minutes": "7"})
    assert "endOffsetMinutes" not in (db.unjs(db.one("SELECT prefs FROM passes")["prefs"]) or {})


def test_removing_a_pass_leaves_what_it_booked(client, tuner, plex):
    pid = _chiefs_pass(client)
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 1
    assert client.post(f"/api/tv/passes/{pid}/delete").json()["ok"] is True
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 0
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 1


def test_one_recording_moves_from_plex_to_couchelephant_and_back(client, synced, tuner):
    aid = _live_airing()
    assert client.post("/api/record", data={"airing_id": aid, "settings": "{}"}).json()["ok"]
    up = client.get("/api/tv/recordings").json()["upcoming"]
    assert [(u["recorder"], u["movable"]) for u in up] == [("plex", True)]

    r = client.post("/api/tv/record/move", data={"airing_id": aid, "recorder": "couchelephant"})
    assert r.json()["ok"] is True, r.json()
    assert db.one("SELECT 1 FROM recordings WHERE airing_id = ?", (aid,))
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (aid,)) is None
    assert fake_plex.STATE.subscriptions == {}

    r = client.post("/api/tv/record/move", data={"airing_id": aid, "recorder": "plex"})
    assert r.json()["ok"] is True, r.json()
    assert db.one("SELECT 1 FROM recordings WHERE airing_id = ?", (aid,)) is None
    assert len(fake_plex.STATE.subscriptions) == 1


def test_a_recording_already_running_stays_with_couchelephant(client, tuner, plex):
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    r = client.post("/api/tv/record/move", data={"airing_id": "a1", "recorder": "plex"})
    assert r.status_code == 409 and "already recording" in r.json()["error"]
    assert recorder.get(rec_id)["state"] == "recording"


def test_changing_who_records_a_pass_moves_what_it_already_booked(client, synced, tuner):
    """Seen on a live install: a team pass switched to CouchElephant kept its
    three booked games in Plex, still marked Recorded by Plex."""
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    passes_mod = __import__("app.passes", fromlist=["x"])
    booked = [d for d in passes_mod.run_passes() if d["action"] == "scheduled"]
    assert booked, "the fixture guide has a game for this team"
    n = len(booked)
    assert len(fake_plex.STATE.subscriptions) == n
    pid = db.one("SELECT id FROM passes")["id"]

    r = client.post(f"/api/tv/passes/{pid}", data={"recorder": "couchelephant"}).json()
    assert r["ok"] is True and f"{n} booked recording(s) moved to CouchElephant" in r["message"]
    assert fake_plex.STATE.subscriptions == {}
    assert db.one("SELECT COUNT(*) c FROM recordings WHERE pass_id = ?", (pid,))["c"] == n
    assert db.one("SELECT COUNT(*) c FROM our_grabs")["c"] == 0
    up = client.get("/api/tv/recordings").json()["upcoming"]
    assert {u["recorder"] for u in up} == {"couchelephant"}

    r = client.post(f"/api/tv/passes/{pid}", data={"recorder": "plex"}).json()
    assert f"{n} booked recording(s) moved to Plex" in r["message"]
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 0
    assert len(fake_plex.STATE.subscriptions) == n


def test_a_recording_that_has_started_does_not_move_with_its_pass(client, tuner, plex):
    pid = _chiefs_pass(client)
    with db.tx() as c:
        c.execute("UPDATE recordings SET state = 'recording'")
    client.post(f"/api/tv/passes/{pid}", data={"recorder": "plex"})
    assert db.one("SELECT state FROM recordings")["state"] == "recording"


# ---------- settings and new passes, from the apps ----------

def test_the_apps_read_settings_without_any_secret(client, tuner):
    db.set_setting("plex_token", "the-real-token")
    db.set_setting("tmdb_key", "the-real-key")
    tv = _pair(client)
    d = client.get("/api/tv/settings", headers=tv).json()
    assert d["may_change"] is True, "sign-in off: anyone may, as in a browser"
    assert d["plex_token_set"] and d["tmdb_key_set"]
    assert "the-real-token" not in str(d) and "the-real-key" not in str(d)
    assert "plex_token" not in d["values"]
    assert d["values"]["live_pause_minutes"] == "30"


def test_the_apps_save_only_what_they_send_and_within_limits(client, tuner):
    tv = _pair(client)
    db.set_setting("recorder_trail_minutes", "3")
    r = client.post("/api/tv/settings", headers=tv,
                    data={"live_pause_minutes": "999", "timezone": "America/Chicago"})
    assert r.json()["ok"], r.json()
    assert db.get_setting("live_pause_minutes") == "240"
    assert db.get_setting("timezone") == "America/Chicago"
    assert db.get_setting("recorder_trail_minutes") == "3", "not sent, not changed"
    bad = client.post("/api/tv/settings", headers=tv, data={"recorder_encoder": "magic"})
    assert bad.status_code == 400
    # Never through here, whatever is sent.
    client.post("/api/tv/settings", headers=tv,
                data={"plex_token": "new", "auth_mode": "none"})
    assert db.get_setting("plex_token") != "new"


def test_with_sign_in_on_a_television_needs_leave_to_change_settings(client, tuner):
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    kid = auth.create_user("kid", "a-good-password", role="user")
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    client.post("/api/tv/devices/approve", data={"code": asked["code"]})
    dev = client.get("/api/tv/devices").json()["devices"][0]["id"]
    client.cookies.clear()
    key = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    tv = {"Authorization": f"Bearer {key}"}

    assert client.get("/api/tv/settings", headers=tv).json()["may_change"] is False
    r = client.post("/api/tv/settings", headers=tv, data={"live_pause_minutes": "60"})
    assert r.status_code == 403
    assert db.get_setting("live_pause_minutes") != "60"

    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    client.post(f"/api/tv/devices/{dev}/settings", data={"allow_settings": "1"})
    row = db.one("SELECT allow_delete, allow_settings FROM tv_devices")
    assert (row["allow_delete"], row["allow_settings"]) == (0, 1), "one flag at a time"
    # A viewer account in a browser is refused, as on the web page.
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(kid))
    assert client.post("/api/tv/settings",
                       data={"live_pause_minutes": "60"}).status_code == 403
    client.cookies.clear()

    assert client.get("/api/tv/hello", headers=tv).json()["can_change_settings"] is True
    assert client.post("/api/tv/settings", headers=tv,
                       data={"live_pause_minutes": "60"}).json()["ok"]
    assert db.get_setting("live_pause_minutes") == "60"


def test_the_web_forms_still_save_through_the_same_rules(client):
    client.post("/settings/recorder", data={"live_pause_minutes": "1",
                                            "recorder_shows_folder": "../etc/tv"})
    assert db.get_setting("live_pause_minutes") == "5"
    assert db.get_setting("recorder_shows_folder") == "etc/tv"


def test_the_plus_button_finds_a_show_and_follows_it(client, synced, tuner, monkeypatch):
    from app.sources import tmdb, tvmaze
    monkeypatch.setattr(tvmaze, "search", lambda q: [])
    monkeypatch.setattr(tmdb, "search", lambda q, key="": [])
    tv = _pair(client)
    found = client.get("/api/tv/passes/find", headers=tv,
                       params={"q": "quiz", "tab": "season"}).json()["results"]
    show = [f for f in found if f["kind"] == "series"][0]
    assert show["title"] == "Quiz Show" and not show["followed"]
    r = client.post("/api/tv/passes/new", headers=tv,
                    data={"kind": "series", "title": show["title"]}).json()
    assert r["ok"] and r["message"].startswith("Following Quiz Show")
    p = db.one("SELECT * FROM passes")
    assert p["series_title"] == "Quiz Show" and p["recorder"] == "couchelephant"
    assert client.get("/api/tv/passes/find", headers=tv).json()["results"] == []
    again = client.get("/api/tv/passes/find", headers=tv,
                       params={"q": "quiz"}).json()["results"]
    assert [f for f in again if f["title"] == "Quiz Show"][0]["followed"]
    assert "Already following" in client.post(
        "/api/tv/passes/new", headers=tv,
        data={"kind": "series", "title": "Quiz Show"}).json()["message"]


def test_the_plus_button_follows_a_team_from_the_catalogue(client, synced, tuner):
    tv = _pair(client)
    found = client.get("/api/tv/passes/find", headers=tv,
                       params={"q": "chiefs", "tab": "sports"}).json()["results"]
    assert found and found[0]["kind"] == "team"
    r = client.post("/api/tv/passes/new", headers=tv,
                    data={"kind": "team", "id": found[0]["id"],
                          "title": found[0]["title"]}).json()
    assert r["ok"], r
    assert db.one("SELECT kind, team_name FROM passes")["team_name"] == found[0]["title"]


def test_a_paired_devices_playlist_plays_with_no_headers_for_airplay(client, tuner):
    """A TV that gets the stream over AirPlay sends no Authorization header,
    so the playlist address asked for at play time carries its own pass."""
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60))
    recorder.tick()
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    uid = recorder.get(rec_id)["uid"]
    _until(lambda: recorder.view_info(uid)["segments"] > 0)

    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    tv = _pair_signed_in(client, admin)
    # The list carries the plain address; the pass comes when play starts.
    card = client.get("/api/tv/home", headers=tv).json()["live_now"][0]
    assert "/hlsk/" not in card["playlist"]
    # An unpaired client gets nothing, from this route or the new ones.
    client.cookies.clear()
    assert client.get(f"/api/tv/recordings/{rec_id}/stream").status_code == 401
    assert client.get("/api/tv/schedule").status_code == 401
    assert client.get("/api/tv/expectations").status_code == 401
    got = client.get(f"/api/tv/recordings/{rec_id}/stream", headers=tv).json()
    assert "/hlsk/" in got["playlist"]
    master = got["playlist"].replace("http://testserver", "")

    assert client.get(master).status_code == 200            # no headers at all
    base = master.rsplit("/", 1)[0]
    assert "seg00000.ts" in client.get(base + "/index.m3u8").text
    assert client.get(base + "/seg00000.ts").status_code == 200

    def refused(path):
        return client.get(path, follow_redirects=False).status_code in (303, 401)
    # Another recording with this pass, a changed pass, and the plain address.
    assert refused(base.rsplit("/", 1)[0] + f"/{'0' * 32}/master.m3u8")
    assert refused(master.replace("/hlsk/", "/hlsk/8."))
    assert refused(f"/hls/{uid}/master.m3u8")
    # An old pass signed the way 1.5.20 first did (with the stored key hash
    # alone) fails closed.
    dev = db.one("SELECT id, key_hash FROM tv_devices")
    exp = int(time.time()) + 3600
    old = hmac.new(dev["key_hash"].encode(), f"{uid}.{exp}".encode(),
                   "sha256").hexdigest()[:32]
    assert refused(f"/hlsk/{dev['id']}.{exp}.{old}/{uid}/master.m3u8")
    # An expired pass, correctly signed, is refused.
    past = int(time.time()) - 5
    sig = tv_routes._stream_sig(dev["id"], dev["key_hash"], uid, past)
    assert refused(f"/hlsk/{dev['id']}.{past}.{sig}/{uid}/master.m3u8")
    # A device given a new key, then a deleted device, loses its passes.
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET key_hash = 'rekeyed' WHERE id = ?", (dev["id"],))
    assert refused(master)
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET key_hash = ? WHERE id = ?",
                  (dev["key_hash"], dev["id"]))
    assert client.get(master).status_code == 200
    with db.tx() as c:
        c.execute("DELETE FROM tv_devices WHERE id = ?", (dev["id"],))
    assert refused(master)


def test_the_access_log_hides_a_video_pass():
    from app.web import hide_pass
    assert hide_pass("/hlsk/3.1795.abcdef/aa/seg00001.ts") == "/hlsk/-/aa/seg00001.ts"
    assert hide_pass("/hls/aa/seg00001.ts") == "/hls/aa/seg00001.ts"


def _pair_signed_in(client, admin):
    asked = client.post("/api/tv/pair", data={"name": "Phone"}).json()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    client.post("/api/tv/devices/approve", data={"code": asked["code"]})
    client.cookies.clear()
    key = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def test_the_schedule_and_what_is_awaited_are_open_to_a_television(client, tuner):
    """The apps' Agenda and Calendar read the web page's own rows."""
    now = int(time.time())
    recorder.book(_airing("a1", "g1", "5.1", now + 3600))
    got = client.get("/api/tv/schedule?limit=10").json()
    assert got["ok"] and got["more"] is False
    row = got["rows"][0]
    assert row["who"] == "ce" and row["channel"]["number"] == "5.1"
    # A window, as a month asks for it.
    assert client.get(f"/api/tv/schedule?start={now + 7200}&end={now + 9000}").json()["rows"] == []
    assert client.get("/api/tv/expectations").json()["ok"] is True
    # No failure recorded for an airing nobody tried: the web's own answer.
    assert client.get("/api/tv/schedule/failure?airing_id=nope").status_code == 404
    assert client.post("/api/tv/schedule/retry", data={"airing_id": "nope"}).status_code in (400, 404)
