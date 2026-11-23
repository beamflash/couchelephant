"""Preferred language, channel languages, and the channels-per-game API."""
import json
import time

from app import db, passes, sync
from tests.test_passes import (
    KC_BUF,
    TELE_GUID,
    _airing_with_teams,
    _bills_game,
    _book,
    _mode_pass,
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
