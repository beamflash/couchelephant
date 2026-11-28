"""Preferred language, channel languages, and the channels-per-game API."""
import json
import time

from app import db, passes, sync
from tests import fake_plex
from tests.test_passes import (
    KC_BUF,
    NBC_GUID,
    TELE_GUID,
    _airing_with_teams,
    _bills_game,
    _book,
    _mode_pass,
    _set_lang,
)

SPANISH_SUMMARY = ("Semana 12 de la temporada regular 2026. En el último enfrentamiento, "
                   "Buffalo Bills venció 28-21 a Kansas City Chiefs, y los dos equipos "
                   "llegan a este partido con una racha de victorias para todos.")
ENGLISH_SUMMARY = ("Week 12 of the 2026 regular season. In their last meeting, the "
                   "Buffalo Bills beat the Kansas City Chiefs 28-21, and both teams "
                   "come into this game with a winning streak for the fans.")


# ---- the preferred language setting ----

def test_a_new_install_prefers_english(client):
    assert db.get_setting("preferred_language") == "en"


def test_setup_saves_the_language_it_was_given(client, plex_url):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    r = client.post("/welcome", data={"plex_url": plex_url, "plex_token": "t",
                                      "timezone": "America/Chicago",
                                      "preferred_language": "es"},
                    headers={"Accept": "application/json"})
    assert r.json()["ok"] is True
    assert db.get_setting("preferred_language") == "es"


def test_setup_refuses_a_language_not_on_the_list(client, plex_url):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    r = client.post("/welcome", data={"plex_url": plex_url, "plex_token": "t",
                                      "preferred_language": "xx"})
    assert r.json()["ok"] is False
    assert db.get_setting("plex_url") == ""


def test_the_setup_page_offers_the_six_languages_in_their_own_words(client):
    db.set_setting("plex_url", "")
    body = client.get("/welcome").text
    for name in ("English", "Español", "Français", "Português", "Deutsch", "Italiano"):
        assert name in body
    assert "Preferred language for TV" in body


def test_settings_can_change_it_and_an_empty_save_leaves_it(client):
    client.post("/settings", data={"timezone": "UTC", "preferred_language": "pt"})
    assert db.get_setting("preferred_language") == "pt"
    client.post("/settings", data={"timezone": "UTC"})
    assert db.get_setting("preferred_language") == "pt", "the Plex tab does not send it"


def test_settings_refuses_a_language_not_on_the_list(client):
    client.post("/settings", data={"timezone": "UTC", "preferred_language": "xx"})
    assert db.get_setting("preferred_language") == "en"


def test_the_app_settings_api_carries_it(client):
    client.post("/api/tv/settings", data={"preferred_language": "fr"})
    j = client.get("/api/tv/settings").json()
    assert j["values"]["preferred_language"] == "fr"
    assert {"code": "es", "name": "Español", "english": "Spanish"} in j["languages"]


# ---- detecting a channel's language from the guide ----

def test_a_sync_detects_a_spanish_channel_and_an_english_one(client, synced):
    kick = int(time.time()) + 3600
    for i in range(6):
        _airing_with_teams(f"es{i}", f"plex://episode/es{i}", "39.1", kick + i * 7200,
                           KC_BUF, f"Partido {i}", "Fútbol Americano NFL")
        _airing_with_teams(f"en{i}", f"plex://episode/en{i}", "41.1", kick + i * 7200,
                           KC_BUF, f"Game {i}", "NFL Football")
        with db.tx() as c:
            c.execute("UPDATE programs SET summary = ? WHERE guid = ?",
                      (SPANISH_SUMMARY, f"plex://episode/es{i}"))
            c.execute("UPDATE programs SET summary = ? WHERE guid = ?",
                      (ENGLISH_SUMMARY, f"plex://episode/en{i}"))
    sync.detect_languages()
    got = {r["vcn"]: r["language"] for r in db.query("SELECT vcn, language FROM channels")}
    assert got["39.1"] == "es" and got["41.1"] == "en"


def test_detection_never_touches_the_users_own_choice(client, synced):
    kick = int(time.time()) + 3600
    for i in range(6):
        _airing_with_teams(f"es{i}", f"plex://episode/es{i}", "39.1", kick + i * 7200,
                           KC_BUF, f"Partido {i}", "Fútbol Americano NFL")
        with db.tx() as c:
            c.execute("UPDATE programs SET summary = ? WHERE guid = ?",
                      (SPANISH_SUMMARY, f"plex://episode/es{i}"))
    with db.tx() as c:
        c.execute("UPDATE channels SET language_override = 'en' WHERE vcn = '39.1'")
    sync.detect_languages()
    row = db.one("SELECT * FROM channels WHERE vcn = '39.1'")
    assert row["language"] == "es" and row["language_override"] == "en"


def test_a_channel_that_cannot_be_read_keeps_what_it_had(client, synced):
    with db.tx() as c:
        c.execute("UPDATE channels SET language = 'es' WHERE vcn = '41.1'")
    sync.detect_languages()          # the fake guide has too little text to call
    assert db.one("SELECT language FROM channels WHERE vcn = '41.1'")["language"] == "es"


# ---- the channel list and the override ----

def test_the_channel_list_carries_all_three_languages(client, synced):
    with db.tx() as c:
        c.execute("UPDATE channels SET language = 'es' WHERE vcn = '41.1'")
    j = client.get("/api/channels/languages").json()
    row = next(c for c in j["channels"] if c["vcn"] == "41.1")
    assert row["language"] == "es"
    assert row["language_detected"] == "es"
    assert row["language_override"] == ""
    assert j["preferred_language"] == "en"
    assert [x["code"] for x in j["languages"]] == ["en", "es", "fr", "pt", "de", "it"]


def test_an_unplaced_channel_counts_as_the_preferred_language(client, synced):
    db.set_setting("preferred_language", "fr")
    row = next(c for c in client.get("/api/channels/languages").json()["channels"]
               if c["vcn"] == "41.1")
    assert row["language"] == "fr" and row["language_detected"] == ""


def test_the_override_is_saved_and_can_be_cleared(client, synced):
    r = client.post("/settings/channels/41.1/language", data={"language_override": "es"})
    assert r.json()["ok"] and r.json()["language"] == "es"
    assert db.one("SELECT language_override o FROM channels WHERE vcn = '41.1'")["o"] == "es"
    r = client.post("/settings/channels/41.1/language", data={"language_override": ""})
    assert r.json()["language_override"] == ""
    assert db.one("SELECT language_override o FROM channels WHERE vcn = '41.1'")["o"] is None


def test_the_override_refuses_a_language_not_on_the_list_or_a_missing_channel(client, synced):
    assert client.post("/settings/channels/41.1/language",
                       data={"language_override": "xx"}).status_code == 400
    assert client.post("/settings/channels/nope/language",
                       data={"language_override": "es"}).status_code == 400


def test_the_apps_can_set_a_channel_language_too(client, synced):
    client.post("/api/tv/settings/channels/41.1/language", data={"language_override": "pt"})
    assert db.one("SELECT language_override o FROM channels WHERE vcn = '41.1'")["o"] == "pt"
    rows = client.get("/api/tv/settings/channels").json()["channels"]
    assert next(c for c in rows if c["vcn"] == "41.1")["language_override"] == "pt"


def test_the_settings_page_lists_each_channel_with_a_language_select(client, synced):
    body = client.get("/partial/settings").text
    assert 'id="lglist"' in body and "Not detected" in body


def test_an_override_travels_in_an_export(client, synced):
    from app import dbstore
    client.post("/settings/channels/41.1/language", data={"language_override": "es"})
    assert dbstore.read("channel_language") == {
        "41.1": {"vcn": "41.1", "language_override": "es"}}


# ---- the pass choice over the API ----

def _team():
    return db.one("SELECT * FROM teams WHERE name LIKE '%Chiefs%'")


def test_a_pass_made_with_the_choice_keeps_it_and_is_ours_not_plexs(client, synced):
    t = _team()
    r = client.post("/api/rules", data={"kind": "team", "team_id": t["id"],
                                        "channels_per_game": "any"})
    assert r.json()["ok"] and r.json()["ce_rule"] is True
    p = db.one("SELECT * FROM passes")
    assert p["channels_per_game"] == "any" and p["team_name"] == t["name"]
    rules = client.get("/api/rules").json()["rules"]
    assert next(x for x in rules if x["who"] == "ce")["channels_per_game"] == "any"


def test_a_pass_with_no_choice_is_one_airing_per_game(client, synced):
    t = _team()
    client.post("/api/rules", data={"kind": "team", "team_id": t["id"],
                                    "networks": json.dumps(["NBC"])})
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "one"


def test_the_choice_can_be_changed_and_an_unsent_one_is_left_alone(client, synced):
    pid = _mode_pass("one")
    client.post(f"/api/rules/{pid}", data={"channels_per_game": "preferred"})
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "preferred"
    client.post(f"/api/rules/{pid}", data={})
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "preferred"


def test_a_bad_choice_is_refused(client, synced):
    pid = _mode_pass("one")
    r = client.post(f"/api/rules/{pid}", data={"channels_per_game": "all"})
    assert r.status_code == 400
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "one"


def test_a_series_pass_cannot_book_several_channels(client, synced):
    r = client.post("/api/rules", data={"kind": "series", "series": "Quiz Show",
                                        "channels_per_game": "any"})
    assert r.status_code == 400


def test_the_apps_pass_list_carries_the_choice(client, synced):
    _mode_pass("preferred")
    rows = client.get("/api/tv/recordings").json()["passes"]
    assert next(p for p in rows if p["who"] == "ce")["channels_per_game"] == "preferred"


# ---- the preview ----

def test_the_preview_says_how_many_recordings_and_how_many_at_once(client, synced):
    _bills_game()
    r = client.post("/api/channels-preview", data={
        "kind": "team", "team": "Kansas City Chiefs", "channels_per_game": "any"}).json()
    assert r["ok"] and r["mode"] == "any"
    assert r["recordings"] == r["recordings_one"] + 1
    assert r["peak"] >= 2
    assert f"books {r['recordings']} recordings instead of {r['recordings_one']}" in r["message"]
    assert f"Up to {r['peak']} at the same time." in r["message"]


def test_the_preview_says_plainly_when_the_tuners_are_not_enough(client, synced, tuner):
    tuner.total = 1
    # The box's count was read when it was added; it now says one.
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET tuner_count = 1")
    _bills_game()
    r = client.post("/api/channels-preview", data={
        "kind": "team", "team": "Kansas City Chiefs", "channels_per_game": "any"}).json()
    assert r["tuners"] == 1 and r["over_tuners"] is True
    assert "You have 1 tuner, so some of them will not all record at once." in r["message"]


def test_the_preview_for_an_existing_pass_reads_its_own_limits(client, synced):
    _bills_game()
    pid = _mode_pass("one")
    with db.tx() as c:
        c.execute("UPDATE passes SET channels = ? WHERE id = ?", (json.dumps(["39.1"]), pid))
    r = client.post("/api/channels-preview", data={
        "rule_id": pid, "channels_per_game": "any"}).json()
    assert r["recordings"] == r["recordings_one"], "one channel allowed, so nothing extra"


def test_the_preview_refuses_a_bad_choice(client, synced):
    assert client.post("/api/channels-preview",
                       data={"kind": "team", "channels_per_game": "all"}).status_code == 400
    assert client.post("/api/channels-preview",
                       data={"kind": "series", "channels_per_game": "any"}).status_code == 400


# ---- the guide-moved repair keeps each channel's booking on its own channel ----

def _same_programme_on_two_channels():
    kick = _bills_game()
    # The same programme, earlier, on a third channel.
    _airing_with_teams("tele2", TELE_GUID, "42.1", kick + 100, KC_BUF,
                       "Kansas City Chiefs vs. Buffalo Bills", "Fútbol Americano NFL")
    return kick


def test_a_repair_never_moves_a_booking_onto_the_other_channel(client, synced):
    kick = _same_programme_on_two_channels()
    pid = _mode_pass("any")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs"), booked_vcn="39.1", pass_id=pid)
    assert sync._would_choose(row)["channel_vcn"] == "39.1"


def test_in_one_mode_a_repair_takes_the_pass_own_first_choice(client, synced):
    kick = _same_programme_on_two_channels()
    pid = _mode_pass("one")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs"), booked_vcn="39.1", pass_id=pid)
    assert sync._would_choose(row)["channel_vcn"] == "42.1"


def test_two_bookings_of_one_programme_on_two_channels_are_not_duplicates_in_any(client, synced):
    kick = _same_programme_on_two_channels()
    pid = _mode_pass("any")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="2")
    _book("tele2", TELE_GUID, "42.1", kick + 100, pid, sub="3")
    with db.tx() as c:
        for key in ("2", "3"):
            c.execute("INSERT INTO plex_grabs (id, subscription, status, channel_vcn, "
                      "begins_at) VALUES (?,?,'scheduled',?,?)",
                      ("g" + key, key, "39.1" if key == "2" else "42.1", kick))
    row = db.one("SELECT * FROM our_grabs WHERE airing_id = 'tele'")
    assert sync._covered_elsewhere(dict(row), same_channel=False) is True
    assert sync._covered_elsewhere(dict(row), same_channel=True) is False


def test_the_modes_are_the_ones_the_docs_name():
    assert passes.MODES == ("one", "preferred", "any")


# ---- stable detection ----

def _listing(vcn, summaries):
    kick = int(time.time()) + 3600
    for i, text in enumerate(summaries):
        _airing_with_teams(f"{vcn}{i}", f"plex://episode/{vcn}{i}", vcn, kick + i * 60,
                           KC_BUF, f"Game {i}", "NFL")
        with db.tx() as c:
            c.execute("UPDATE programs SET summary = ? WHERE guid = ?",
                      (text, f"plex://episode/{vcn}{i}"))


def test_a_borderline_channel_does_not_flip_between_syncs(client, synced):
    es = [SPANISH_SUMMARY] * 7
    en = [ENGLISH_SUMMARY] * 4
    _listing("39.1", es + en[:2])
    sync.detect_languages()
    first = db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"]
    assert first == "es"
    # The sample shifts so English has a little more of it. Not a clear verdict.
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE channel_vcn = '39.1'")
    _listing("39.1", es[:3] + en)
    sync.detect_languages()
    assert db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"] == "es"


def test_a_clear_change_does_overturn_the_old_language(client, synced):
    _listing("39.1", [SPANISH_SUMMARY] * 6)
    sync.detect_languages()
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE channel_vcn = '39.1'")
    _listing("39.1", [ENGLISH_SUMMARY] * 8)
    sync.detect_languages()
    assert db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"] == "en"


def test_the_sample_is_taken_in_a_fixed_order(client, synced):
    _listing("39.1", [SPANISH_SUMMARY] * 4)
    sync.detect_languages()
    again = db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"]
    sync.detect_languages()
    assert db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"] == again


# ---- deleting a record from a store that is only some columns of a row ----

def test_a_replace_import_clears_a_channel_language_and_keeps_the_channel(client, synced):
    from app import portable
    client.post("/settings/channels/41.1/language", data={"language_override": "es"})
    blob = portable.export_bytes()
    client.post("/settings/channels/38.1/language", data={"language_override": "fr"})
    portable.import_bytes(blob, replace=True)
    rows = {r["vcn"]: r for r in db.query("SELECT * FROM channels")}
    assert "38.1" in rows, "the channel itself is the guide's and stays"
    assert rows["38.1"]["language_override"] is None
    assert rows["41.1"]["language_override"] == "es"


def test_a_replace_import_clears_channel_artwork_and_keeps_the_channel(client, synced):
    from app import portable
    png = b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * 64
    blob = portable.export_bytes()
    client.post("/settings/channels/41.1/logo", files={"logo": ("x.png", png, "image/png")})
    portable.import_bytes(blob, replace=True)
    row = db.one("SELECT * FROM channels WHERE vcn = '41.1'")
    assert row is not None and not row["custom_logo"]


def test_a_backing_store_delete_clears_the_columns_and_keeps_the_channel(
        client, synced, tmp_path):
    from app import backingstore
    db.set_setting("backingstore_backend", "sqlite")
    db.set_setting("sqlite_path", str(tmp_path / "backing.db"))
    client.post("/settings/channels/41.1/language", data={"language_override": "es"})
    backingstore.sync_all()
    # Removed here, so the merge sends the delete on and must not drop the row.
    with db.tx() as c:
        c.execute("UPDATE channels SET language_override = NULL WHERE vcn = '41.1'")
    backingstore.sync_all()
    assert db.one("SELECT 1 FROM channels WHERE vcn = '41.1'")
    # Removed in the store, so the merge deletes it here.
    client.post("/settings/channels/38.1/language", data={"language_override": "fr"})
    backingstore.sync_all()
    backingstore.chosen().write("channel_language", {}, ["38.1"])
    backingstore.sync_all()
    row = db.one("SELECT * FROM channels WHERE vcn = '38.1'")
    assert row is not None and row["language_override"] is None


# ---- repair with the real check ----

def test_a_moved_game_is_repaired_in_any_mode_and_not_booked_twice(plex, synced):
    from tests.test_guide_moved import _guide_says, _kickoff, _live_subs
    was = _kickoff(plex)
    with db.tx() as c:
        team = db.one("SELECT * FROM teams WHERE name LIKE '%Chiefs%'")
        c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, prefs, "
                  "uid, enabled, created_at, channels_per_game) "
                  "VALUES ('team',?,?,'[]','[]','{}','u1',1,0,'any')",
                  (team["id"], team["name"]))
    passes.run_passes()
    assert len(_live_subs()) >= 1
    n = len(_live_subs())
    _guide_says(plex, was + 15 * 60)
    out = sync.check_bookings(plex)
    assert out["repaired"] >= 1 and out["cancelled"] == 0
    passes.run_passes()
    assert len(_live_subs()) == n


# ---- stand-ins in the modes that book several channels ----

def test_a_stand_in_is_rebooked_onto_a_channel_the_pass_is_not_booking(client, synced):
    kick = _bills_game()
    pid = _mode_pass("any")
    # The pass already books the NBC airing; its stand-in sat on another slot.
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    named = sync._would_choose(row, stand_in=True)
    assert named["channel_vcn"] == "39.1", "the channel not already booked"
    assert sync._would_choose(row) is None or \
        sync._would_choose(row)["channel_vcn"] == "38.1"


# ---- stand-ins and language ----

def test_a_stand_in_in_preferred_never_moves_to_the_other_language(client, synced):
    kick = _bills_game()
    pid = _mode_pass("preferred")
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    # The guide names the game on 39.1 (Spanish) only, and on 41.1, which is taken.
    _airing_with_teams("tele_b", TELE_GUID, "41.1", kick + 120, KC_BUF, "A", "NFL")
    got = sync._would_choose(row, stand_in=True)
    assert got is False, "English is already booked, so the stand-in is redundant"


def test_a_stand_in_in_preferred_falls_back_when_no_channel_is_in_the_language(client, synced):
    kick = _bills_game()
    pid = _mode_pass("preferred")
    db.set_setting("preferred_language", "fr")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs"), booked_vcn="38.1", program_guid=TELE_GUID)
    # The whole game is seen now, so the fallback is the pass's own first choice.
    assert sync._would_choose(row, stand_in=True)["channel_vcn"] == "41.1"


def test_a_stand_in_counts_our_own_recorders_bookings_as_taken(client, synced):
    kick = _bills_game()
    pid = _mode_pass("any")
    with db.tx() as c:
        c.execute("INSERT INTO recordings (uid, kind, state, program_guid, channel_vcn, "
                  "begins_at, starts_at, source, pass_id) VALUES "
                  "('u-r','recording','scheduled',?,'39.1',?,?,'pass',?)",
                  (TELE_GUID, kick + 300, kick + 300, pid))
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    assert sync._would_choose(row, stand_in=True) is False


def test_a_three_to_one_flip_is_rejected(client, synced):
    _listing("39.1", [SPANISH_SUMMARY] * 7 + [ENGLISH_SUMMARY] * 2)
    sync.detect_languages()
    assert db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"] == "es"
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE channel_vcn = '39.1'")
    _listing("39.1", [SPANISH_SUMMARY] * 3 + [ENGLISH_SUMMARY] * 4)
    from app import language
    t = [SPANISH_SUMMARY] * 3 + [ENGLISH_SUMMARY] * 4
    assert language.detect(t) == "en" and language.detect(t, decisive=True) is None
    sync.detect_languages()
    assert db.one("SELECT language FROM channels WHERE vcn = '39.1'")["language"] == "es"


def test_removing_channel_artwork_deletes_the_file_but_never_one_outside(client, synced, tmp_path):
    import os

    from app import dbstore, paths
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    client.post("/settings/channels/41.1/logo", files={"logo": ("x.png", png, "image/png")})
    path = db.one("SELECT custom_logo FROM channels WHERE vcn = '41.1'")["custom_logo"]
    assert os.path.isfile(path)
    spec = dbstore.STORES["channel_art"]
    dbstore.remove_row(dbstore._con("main"), spec, spec["pk"], ["41.1"])
    assert not os.path.exists(path)
    outside = tmp_path / "keep.png"
    outside.write_bytes(png)
    with db.tx() as c:
        c.execute("UPDATE channels SET custom_logo = ? WHERE vcn = '41.1'", (str(outside),))
    dbstore.remove_row(dbstore._con("main"), spec, spec["pk"], ["41.1"])
    assert outside.exists(), "only files inside the logo folder are removed"
    assert paths.logos_dir()


# ---- a stand-in sees the whole game, not one listing of it ----

def _tele_earlier_with_stand_in(mode):
    kick = _bills_game()
    with db.tx() as c:      # Telemundo kicks off first, so its guid is the matched one
        c.execute("UPDATE airings SET begins_at = ? WHERE id = 'tele'", (kick - 300,))
    db.set_setting("preferred_language", "en")
    pid = _mode_pass(mode)
    return kick, pid


def test_a_one_mode_stand_in_named_on_the_telemundo_guid_lands_on_nbc(client, synced):
    kick, pid = _tele_earlier_with_stand_in("one")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    assert sync._would_choose(row, stand_in=True)["channel_vcn"] == "41.1"


def test_a_preferred_stand_in_named_on_the_telemundo_guid_is_redundant_when_nbc_is_booked(
        client, synced):
    kick, pid = _tele_earlier_with_stand_in("preferred")
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    assert sync._would_choose(row, stand_in=True) is False


def test_the_fallback_does_not_book_a_second_channel_when_one_is_taken(client, synced):
    kick = _bills_game()
    _airing_with_teams("tele2", "plex://episode/tele2", "40.1", kick + 120, KC_BUF, "B", "NFL")
    _set_lang("40.1", "es")
    db.set_setting("preferred_language", "fr")        # no channel is in French
    pid = _mode_pass("preferred")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="1")
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    row = dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
               booked_vcn="38.1", program_guid=TELE_GUID)
    assert sync._would_choose(row, stand_in=True) is False


# ---- the language window ----

def _same_listing(english_at):
    """Telemundo live at kick, and the same listing in English `english_at` later."""
    kick = _bills_game()
    with db.tx() as c:
        c.execute("UPDATE airings SET program_guid = ?, premiere = 1, begins_at = ?, "
                  "ends_at = ? WHERE id = 'tele'", (NBC_GUID, kick, kick + 3600))
        c.execute("UPDATE airings SET premiere = 0, begins_at = ?, ends_at = ? "
                  "WHERE id = 'nbc'", (kick + english_at, kick + english_at + 3600))
    db.set_setting("preferred_language", "en")
    return kick


def test_english_two_hours_later_beats_spanish_live_in_one_mode(plex, synced):
    from tests.test_passes import _dry_channels
    _same_listing(2 * 3600)
    _mode_pass("one")
    assert _dry_channels(3 * 3600) == ["41.1"]


def test_english_the_next_day_does_not_beat_spanish_live(plex, synced):
    from tests.test_passes import _dry_channels
    _same_listing(24 * 3600)
    _mode_pass("one")
    assert _dry_channels(2 * 86400) == ["39.1"]


def test_any_still_books_only_the_airings_of_the_live_game(plex, synced):
    from tests.test_passes import _dry_channels
    _same_listing(2 * 3600)
    _mode_pass("any")
    assert _dry_channels() == ["39.1"], "the evening repeat is not another channel of the game"


def test_a_symlinked_logo_is_never_deleted(client, synced, tmp_path):
    import os

    from app import dbstore, paths
    target = tmp_path / "precious.png"
    target.write_bytes(b"x")
    link = os.path.join(paths.logos_dir(), "link.png")
    os.symlink(target, link)
    try:
        with db.tx() as c:
            c.execute("UPDATE channels SET custom_logo = ? WHERE vcn = '41.1'", (link,))
        spec = dbstore.STORES["channel_art"]
        dbstore.remove_row(dbstore._con("main"), spec, spec["pk"], ["41.1"])
        assert target.exists() and os.path.islink(link)
    finally:
        os.remove(link)


# ---- the stand-in, the pass run and the keeper agree ----

def _stand_row(guid):
    return dict(db.one("SELECT * FROM our_grabs WHERE airing_id = 'stand'"),
                booked_vcn="38.1", program_guid=guid)


def test_pass_run_stand_in_and_keeper_pick_the_same_airing_in_one_guid(plex, synced):
    from tests.test_passes import _dry_channels
    kick = _same_listing(2 * 3600)
    pid = _mode_pass("one")
    run = _dry_channels(3 * 3600)
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    got = sync._would_choose(_stand_row(NBC_GUID), stand_in=True)
    assert run == ["41.1"] and got["channel_vcn"] == "41.1"
    with db.tx() as c:
        c.execute("DELETE FROM our_grabs")
    _book("tele", NBC_GUID, "39.1", kick, pid, sub="1")
    _book("nbc", NBC_GUID, "41.1", kick + 7200, pid, sub="3")
    assert passes._keeper(passes._pass_bookings(pid), "en")["channel_vcn"] == "41.1"


def test_a_stand_in_agrees_with_the_run_across_language_mixes(plex, synced):
    from tests.test_passes import _dry_channels
    kick = _bills_game()
    _airing_with_teams("cbs", "plex://episode/cbs", "42.1", kick + 120, KC_BUF, "B", "NFL")
    db.set_setting("preferred_language", "en")
    pid = _mode_pass("one")
    for l41, l42 in (("en", "en"), (None, "en"), (None, None), ("en", None)):
        _set_lang("41.1", l41)
        _set_lang("42.1", l42)
        run = _dry_channels()
        _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="9")
        got = sync._would_choose(_stand_row(TELE_GUID), stand_in=True)
        with db.tx() as c:
            c.execute("DELETE FROM our_grabs WHERE airing_id = 'stand'")
        assert run == [got["channel_vcn"]], (l41, l42)


# ---- one game across languages, up to six hours apart ----

def _delayed_english(gap):
    """Telemundo live under its own guid, NBC `gap` later under another."""
    kick = _bills_game()
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ?, ends_at = ? WHERE id = 'tele'",
                  (kick, kick + 3600))
        c.execute("UPDATE airings SET premiere = 0, begins_at = ?, ends_at = ? "
                  "WHERE id = 'nbc'", (kick + gap, kick + gap + 3600))
    db.set_setting("preferred_language", "en")
    return kick


def _games(mode="one"):
    return passes.group_by_game(
        passes.candidate_airings(159, team_name="Kansas City Chiefs"), mode)


def test_the_delayed_english_broadcast_and_the_live_spanish_one_are_one_game(plex, synced):
    from tests.test_passes import _dry_channels
    kick = _delayed_english(2 * 3600)
    games = [g for g in _games().values() if any(a["id"] in ("nbc", "tele") for a in g)]
    assert len(games) == 1
    _mode_pass("one")
    assert _dry_channels(3 * 3600) == ["41.1"], "one booking, and it is the English one"
    assert kick


def test_an_english_doubleheader_three_hours_apart_is_still_two_games(plex, synced):
    kick = _bills_game()
    _set_lang("39.1", "en")
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ?, ends_at = ? WHERE id = 'tele'",
                  (kick + 3 * 3600, kick + 4 * 3600))
    games = [g for g in _games().values() if any(a["id"] in ("nbc", "tele") for a in g)]
    assert len(games) == 2


def test_a_spanish_doubleheader_on_telemundo_is_still_two_games(plex, synced):
    kick = _bills_game()
    _airing_with_teams("tele2", "plex://episode/tele-g2", "39.1", kick + 3 * 3600, KC_BUF,
                       "Kansas City Chiefs vs. Buffalo Bills 2", "Fútbol Americano NFL")
    games = [g for g in _games().values() if any(a["id"] in ("tele", "tele2") for a in g)]
    assert len(games) == 2
    assert not any({"tele", "tele2"} <= {a["id"] for a in g} for g in games)


def test_languages_further_apart_than_six_hours_are_two_games(plex, synced):
    _delayed_english(7 * 3600)
    games = [g for g in _games().values() if any(a["id"] in ("nbc", "tele") for a in g)]
    assert len(games) == 2


# ---- the stand-in's anchor ----

def test_a_stand_in_anchors_on_the_day_the_game_was_expected_not_a_replay(plex, synced):
    kick = _bills_game()
    # The named guid also replays next day on 42.1; the game itself was at kick.
    _airing_with_teams("replay", NBC_GUID, "42.1", kick + 86400, KC_BUF, "A", "NFL")
    _set_lang("42.1", "en")
    db.set_setting("preferred_language", "en")
    pid = _mode_pass("one")
    # The first future airing has already started, so only the replay is "earliest future".
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE id IN ('nbc', 'tele')")
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, subtitle, "
                  "expected_at, precision, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (pid, "thesportsdb", "x", "Kansas City Chiefs", "m", kick, "time", 0))
    exp = db.one("SELECT id FROM expectations")["id"]
    _airing_with_teams("nbc", NBC_GUID, "41.1", kick - 100, KC_BUF, "A", "NFL")
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ? WHERE id = 'nbc'", (kick + 200,))
    _book("stand", "plex://episode/generic", "38.1", kick, pid, sub="2")
    with db.tx() as c:
        c.execute("UPDATE our_grabs SET expectation_id = ? WHERE airing_id = 'stand'", (exp,))
    row = _stand_row(NBC_GUID)
    got = sync._would_choose(row, stand_in=True)
    assert got["channel_vcn"] == "41.1" and got["begins_at"] == kick + 200


# ---- the narrowed language merge ----

def _two_guids(first, second, gap=3 * 3600):
    """Game 1 on `first`, a second guid `gap` later on `second`, both premieres.
    Each is (vcn, language). Telemundo and NBC are 39.1 and 41.1."""
    kick = _bills_game()
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE id IN ('nbc', 'tele')")
    (v1, l1), (v2, l2) = first, second
    _airing_with_teams("g1", NBC_GUID, v1, kick, KC_BUF, "G1", "NFL")
    _airing_with_teams("g2", TELE_GUID, v2, kick + gap, KC_BUF, "G2", "NFL")
    _set_lang(v1, l1)
    if v2 != v1:
        _set_lang(v2, l2)
    db.set_setting("preferred_language", "en")
    return kick


def _ids(game_list):
    return sorted(sorted(a["id"] for a in g) for g in game_list.values()
                  if any(a["id"] in ("g1", "g2") for a in g))


def test_a_cross_language_doubleheader_with_game_two_on_nbc_is_two_games(plex, synced):
    from tests.test_passes import _dry_channels
    _two_guids(("39.1", "es"), ("41.1", "en"))
    assert _ids(_games("one")) == [["g1"], ["g2"]]
    _mode_pass("one")
    assert _dry_channels(4 * 3600) == ["39.1", "41.1"]


def test_a_cross_language_doubleheader_with_game_two_on_telemundo_is_two_games(plex, synced):
    from tests.test_passes import _dry_channels
    _two_guids(("41.1", "en"), ("39.1", "es"))
    assert _ids(_games("one")) == [["g1"], ["g2"]]
    _mode_pass("one")
    assert _dry_channels(4 * 3600) == ["39.1", "41.1"]


def test_an_unknown_language_never_merges_games(plex, synced):
    from tests.test_passes import _dry_channels
    _two_guids(("39.1", None), ("41.1", "en"))
    assert _ids(_games("one")) == [["g1"], ["g2"]]
    _mode_pass("one")
    assert _dry_channels(4 * 3600) == ["39.1", "41.1"]


def test_a_delayed_non_premiere_nbc_still_joins_the_live_telemundo_game(plex, synced):
    from tests.test_passes import _dry_channels
    _two_guids(("39.1", "es"), ("41.1", "en"), gap=2 * 3600)
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'g2'")
    assert _ids(_games("one")) == [["g1", "g2"]]
    _mode_pass("one")
    assert _dry_channels(3 * 3600) == ["41.1"]


def test_any_books_the_live_telemundo_and_the_delayed_nbc(plex, synced):
    from tests.test_passes import _dry_channels
    _two_guids(("39.1", "es"), ("41.1", "en"), gap=2 * 3600)
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'g2'")
    assert _ids(_games("any")) == [["g1"], ["g2"]]
    _mode_pass("any")
    assert _dry_channels(3 * 3600) == ["39.1", "41.1"]


def test_a_replay_days_later_does_not_change_which_languages_a_programme_has(plex, synced):
    kick = _two_guids(("39.1", "es"), ("41.1", "en"), gap=2 * 3600)
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'g2'")
    # The Telemundo guid is shown on an English channel ten days later: that must
    # not make the game "already English" and keep the delayed NBC apart.
    _airing_with_teams("g1b", NBC_GUID, "43.1", kick + 10 * 86400, KC_BUF, "G1", "NFL")
    _set_lang("43.1", "en")
    assert _ids(_games("one")) == [["g1", "g1b", "g2"]]


def test_dedupe_never_merges_by_language_so_two_real_games_are_both_kept(plex):
    pid = _mode_pass("one")
    kick = _two_guids(("39.1", "es"), ("41.1", "en"), gap=2 * 3600)
    _book("g1", NBC_GUID, "39.1", kick, pid, sub="1")
    _book("g2", TELE_GUID, "41.1", kick + 2 * 3600, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})
    assert passes.dedupe_games() == []
    assert len(fake_plex.STATE.subscriptions) == 2
    got = {b["channel_vcn"]: b["channel_language"] for b in passes._pass_bookings(pid)}
    assert got == {"39.1": "es", "41.1": "en"}


# ---- a game whose first airing has started ----

def _under_way(nbc_ago, tele_in, tele_premiere=1):
    """NBC began `nbc_ago` seconds ago and is being recorded by Plex. Telemundo
    airs the game under its own guid `tele_in` seconds from now."""
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - nbc_ago, KC_BUF, "A at B", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", now + tele_in, KC_BUF, "A vs. B", "NFL")
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = ? WHERE id = 'tele'", (tele_premiere,))
        c.execute("INSERT INTO plex_grabs (id, subscription, status, channel_vcn, begins_at) "
                  "VALUES ('g1', '1', 'inprogress', '41.1', ?)", (now - nbc_ago,))
    _set_lang("41.1", "en")
    _set_lang("39.1", "es")
    db.set_setting("preferred_language", "en")
    return now


def _runs(mode, lo, hi):
    db.set_setting("dry_run", "1")
    _mode_pass(mode)
    return sorted(d["channel"] for d in passes.run_passes()
                  if d.get("action") == "would schedule"
                  and d["game"] in ("A at B", "A vs. B") and lo <= d["begins_at"] <= hi)


def test_a_run_just_after_nbc_starts_does_not_book_telemundo(plex, synced):
    now = _under_way(nbc_ago=130, tele_in=170)        # NBC T, Telemundo T+300
    assert _runs("one", now - 200, now + 400) == []


def test_a_run_during_a_delayed_broadcast_does_not_book_the_repeat(plex, synced):
    now = _under_way(nbc_ago=600, tele_in=2 * 3600 - 600, tele_premiere=0)
    assert _runs("one", now - 700, now + 3 * 3600) == []


def test_a_run_two_hours_in_still_books_nothing_new(plex, synced):
    now = _under_way(nbc_ago=2 * 3600, tele_in=30, tele_premiere=0)
    assert _runs("one", now - 3 * 3600, now + 3600) == []
    assert _runs("preferred", now - 3 * 3600, now + 3600) == []


def test_any_still_books_the_other_channel_while_nbc_records(plex, synced):
    now = _under_way(nbc_ago=130, tele_in=170)
    assert _runs("any", now - 200, now + 400) == ["39.1"]


def test_an_in_progress_recording_of_ours_counts_as_handled(plex):
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - 600, KC_BUF, "A", "NFL")
    with db.tx() as c:
        c.execute("INSERT INTO recordings (uid, kind, state, program_guid, channel_vcn, "
                  "begins_at, starts_at, source) VALUES "
                  "('u1','recording','recording',?,'41.1',?,?,'pass')",
                  (NBC_GUID, now - 600, now - 600))
    assert passes.already_handled([NBC_GUID, TELE_GUID]) == "CouchElephant is recording it"


def test_a_plex_grab_in_progress_counts_as_handled(plex, synced):
    _under_way(nbc_ago=600, tele_in=3600)
    assert passes.already_handled([NBC_GUID, TELE_GUID]).startswith("Plex already has it")


def test_a_pass_made_mid_game_books_the_later_airing_when_nothing_is_recorded(plex, synced):
    """Documented rule: in one, the later airing is booked only when no airing
    of the game is being recorded. Nothing is, so Telemundo is the game's only
    way to be recorded and is booked."""
    now = _under_way(nbc_ago=600, tele_in=1800, tele_premiere=0)
    with db.tx() as c:
        c.execute("DELETE FROM plex_grabs")
    assert _runs("one", now - 700, now + 3600) == ["39.1"]


def test_dedupe_sees_an_in_progress_booking_and_never_keeps_two_or_cancels_it(plex):
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - 600, KC_BUF, "A", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", now + 1800, KC_BUF, "B", "NFL")
    pid = _mode_pass("one")
    _book("nbc", NBC_GUID, "41.1", now - 600, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", now + 1800, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})
    with db.tx() as c:
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('1')")
    # Telemundo is the better-ranked booking, but the one in progress stays.
    _set_lang("41.1", "es")
    _set_lang("39.1", "en")
    out = passes.dedupe_games()
    assert [d["action"] for d in out] == ["cancelled"]
    assert list(fake_plex.STATE.subscriptions) == ["1"]


def test_dedupe_never_cancels_an_in_progress_booking_even_when_it_is_the_extra(plex):
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - 600, KC_BUF, "A", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", now - 400, KC_BUF, "B", "NFL")
    pid = _mode_pass("one")
    _book("nbc", NBC_GUID, "41.1", now - 600, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", now - 400, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})
    with db.tx() as c:
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('1')")
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('2')")
    assert passes.dedupe_games() == []
    assert len(fake_plex.STATE.subscriptions) == 2


# ---- a lost started booking, and the Upcoming panel ----

def test_a_lost_started_booking_does_not_make_the_pass_churn(plex, synced):
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - 600, KC_BUF, "A at B", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", now + 1800, KC_BUF, "A vs. B", "NFL")
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'tele'")
    _set_lang("41.1", "en")
    _set_lang("39.1", "es")
    db.set_setting("preferred_language", "en")
    db.set_setting("dry_run", "0")
    pid = _mode_pass("one")
    _book("nbc", NBC_GUID, "41.1", now - 600, pid, sub="1")     # Plex lost it

    def mine(out):
        return [(d["action"], d["channel"]) for d in out
                if d.get("game") in ("A at B", "A vs. B") and d["action"] != "skipped"]

    db.set_setting("dry_run", "1")
    assert ("would schedule", "39.1") in mine(passes.run_passes()), "the game is not lost"
    # Telemundo is booked (the fake Plex cannot book these airings itself).
    _book("tele", TELE_GUID, "39.1", now + 1800, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"2": {"key": "2"}})
    with db.tx() as c:
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('2')")
    for _ in range(3):
        assert passes.dedupe_games() == [], "the dead NBC row is not a keeper"
        assert mine(passes.run_passes()) == []
    assert list(fake_plex.STATE.subscriptions) == ["2"]


def test_a_started_booking_plex_still_holds_is_kept(plex, synced):
    now = int(time.time())
    _airing_with_teams("nbc", NBC_GUID, "41.1", now - 600, KC_BUF, "A at B", "NFL")
    pid = _mode_pass("one")
    _book("nbc", NBC_GUID, "41.1", now - 600, pid, sub="1")
    with db.tx() as c:
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('1')")
    assert [b["id"] for b in passes._pass_bookings(pid)] == ["nbc"]


def test_the_upcoming_panel_agrees_with_the_run_while_nbc_records(client, synced):
    _under_way(nbc_ago=130, tele_in=170)
    pid = _mode_pass("one")
    rows = client.get(f"/api/rules/{pid}/upcoming").json()["upcoming"]
    ours = [u for u in rows if u["title"] in ("A at B", "A vs. B")]
    assert all(u["status"] != "will schedule" for u in ours), ours
    assert not any(u["vcn"] == "41.1" and u["status"] == "will schedule" for u in ours)
