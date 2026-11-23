"""The selection logic. This is the product, so it gets the most tests."""
import json
import time

from app import db, passes
from tests import fake_plex


def _airing(aid, guid, vcn, begins, premiere=0, drm=0, title="Game"):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  (guid, title, "rk", json.dumps([{"id": 236, "name": "Chiefs"}]),
                   "NFL Football", "sports"))
        c.execute("INSERT OR IGNORE INTO channels (vcn, network, call_sign) VALUES (?,?,?)",
                  (vcn, {"41.1": "NBC", "38.1": "Independent",
                         "9.1": "ABC", "5.1": "CBS"}.get(vcn), vcn))
        c.execute("""INSERT OR REPLACE INTO airings
                     (id, program_guid, channel_vcn, channel_identifier, begins_at,
                      ends_at, premiere, drm) VALUES (?,?,?,?,?,?,?,?)""",
                  (aid, guid, vcn, f"id-{vcn}", begins, begins + 3600, premiere, drm))


def _rows(guid):
    return db.query("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams,
                              p.summary, c.network AS channel_network
                       FROM airings a JOIN programs p ON p.guid = a.program_guid
                       LEFT JOIN channels c ON c.vcn = a.channel_vcn
                       WHERE a.program_guid = ? ORDER BY a.begins_at""", (guid,))


def test_prefers_the_premiere_over_an_earlier_repeat():
    """The whole point. Plex breaks the tie on channel number and gets this
    wrong; a repeat that airs FIRST must still lose to the flagged live one."""
    g = "plex://episode/g"
    _airing("a1", g, "38.1", 1000, premiere=0)     # earlier, but a repeat
    _airing("a2", g, "41.1", 2000, premiere=1)     # later, and live
    pick, why = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"
    assert "premiere" in why


def test_takes_the_earliest_premiere_when_several_are_flagged():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 3000, premiere=1)
    _airing("a2", g, "38.1", 2000, premiere=1)
    pick, _ = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"


def test_falls_back_to_the_earliest_and_says_so():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 3000)
    _airing("a2", g, "38.1", 2000)
    pick, why = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"
    assert "no airing is flagged premiere" in why


def test_drm_airings_are_never_chosen():
    g = "plex://episode/g"
    _airing("a1", g, "5.1", 1000, premiere=1, drm=1)
    _airing("a2", g, "41.1", 2000)
    pick, _ = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2", "a DRM premiere must lose to a recordable repeat"


def test_all_drm_is_refused_with_a_reason():
    g = "plex://episode/g"
    _airing("a1", g, "5.1", 1000, premiere=1, drm=1)
    pick, why = passes.choose_airing(_rows(g))
    assert pick is None
    assert "DRM" in why


# ---- source limits ----

def _rule(networks=None, channels=None):
    return {"networks": json.dumps(networks or []),
            "channels": json.dumps(channels or []),
            "kind": "team", "team_id": 236, "series_guid": None,
            "series_title": None}


def test_no_limit_allows_anywhere():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)
    nets, chans = passes.allowed_sources(_rule())
    assert passes.in_sources(_rows(g)[0], nets, chans)


def test_a_network_limit_admits_only_that_network():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)      # NBC
    _airing("a2", g, "9.1", 2000)       # ABC
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"]))
    rows = _rows(g)
    assert not passes.in_sources(rows[0], nets, chans)
    assert passes.in_sources(rows[1], nets, chans)


def test_networks_and_channels_are_one_allowlist_not_two_filters():
    """Naming a network and a channel means either of them."""
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)      # NBC, named by channel
    _airing("a2", g, "9.1", 2000)       # ABC, named by network
    _airing("a3", g, "5.1", 3000)       # CBS, named by neither
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"], channels=["41.1"]))
    got = [r["id"] for r in _rows(g) if passes.in_sources(r, nets, chans)]
    assert got == ["a1", "a2"]


def test_the_limit_is_applied_before_the_choice():
    """Filtering after choosing would pick the live airing and then find it
    disallowed, and book nothing. The best ALLOWED airing must win."""
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)    # live, but on NBC
    _airing("a2", g, "9.1", 5000)                 # a repeat, on ABC
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"]))
    allowed = [a for a in _rows(g) if passes.in_sources(a, nets, chans)]
    pick, _ = passes.choose_airing(allowed)
    assert pick["id"] == "a2"


# ---- already handled ----

def test_a_repeated_title_does_not_block_a_later_broadcast():
    """B6. A daily programme keeps its title, so matching on title alone made
    every future airing read as already recorded."""
    g1, g2 = "plex://episode/day1", "plex://episode/day2"
    _airing("a1", g1, "9.1", 1000, title="Quiz Night")
    _airing("a2", g2, "9.1", 90000, title="Quiz Night")
    with db.tx() as c:
        c.execute("""INSERT INTO plex_grabs (id, status, title, channel_vcn,
                                             begins_at, updated_at)
                     VALUES ('g1','complete','Quiz Night','9.1',1000,0)""")
    assert passes.already_handled(g1), "the broadcast that was recorded is covered"
    assert passes.already_handled(g2) is None, "tomorrow's is not"


def test_a_pass_that_already_booked_a_game_says_so():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title,
                                            channel_vcn, begins_at, source,
                                            subscription, created_at)
                     VALUES ('a1',?,'Game','41.1',1000,'pass','7',0)""", (g,))
        c.execute("""INSERT INTO plex_subscriptions (key, title, type, created_at,
                                                     updated_at, owned_by_us)
                     VALUES ('7','This Event','4',0,0,1)""")
    assert "already booked by a pass" == passes.already_handled(g)


def test_having_once_scheduled_a_game_is_not_a_reason_to_ignore_it():
    """The log is not the live state, and reading it as one loses recordings.

    A booking can be lost after it is made: Plex drops a subscription on its
    own, the user cancels one, or the guide re-times the broadcast and the
    pinned recording stops existing. This used to answer "already scheduled"
    to all three, for the life of the install, so the pass never looked at the
    game again and nothing recorded it.
    """
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO sync_log (started_at, ended_at, ok)
                     VALUES (500, 500, 1)""")
        c.execute("""INSERT INTO pass_actions (pass_id, program_guid, action,
                                               dry_run, created_at)
                     VALUES (1, ?, 'scheduled', 0, 100)""", (g,))
    assert passes.already_handled(g) is None


def test_a_booking_made_since_the_last_sync_still_counts():
    """The other half of it. Plex is only read back by a sync, so between
    booking a game and the next sync nothing else knows the recording exists.
    Without this, two runs of a pass in a row would book it twice.
    """
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO sync_log (started_at, ended_at, ok)
                     VALUES (500, 500, 1)""")
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title,
                                            channel_vcn, begins_at, source,
                                            subscription, created_at)
                     VALUES ('a1',?,'Game','41.1',1000,'pass','7',900)""", (g,))
    assert "already booked by a pass" == passes.already_handled(g)


# ---- booking ----

def test_a_booking_is_pinned_to_the_chosen_broadcast(plex, synced):
    """Without the pin Plex re-chooses, which is the bug the app exists for."""
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test")
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["lineupChannel"] == pick["channel_identifier"]


def test_a_user_cannot_unpin_a_pass_booking(plex, synced):
    """The three pinning prefs are not the caller's to override."""
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test",
                     prefs={"oneShot": "0", "startTimeslot": "-1",
                            "lineupChannel": "", "startOffsetMinutes": "5"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["startOffsetMinutes"] == "5", "other settings still apply"


def test_a_discarded_create_is_reported_as_a_failure(plex, synced):
    """Reporting a recording Plex did not keep is worse than failing."""
    import pytest

    from app.plex import PlexError
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    fake_plex.STATE.drop_next_create = True
    with pytest.raises(PlexError, match="discarded"):
        passes._schedule(plex, pick, None, "test")
    assert not db.query("SELECT 1 FROM our_grabs"), "nothing may be remembered"


def test_run_passes_books_the_live_broadcast(plex, synced):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 236, 'Kansas City Chiefs', 1, ?)", (int(time.time()),))
    out = passes.run_passes()
    booked = [d for d in out if d["action"] == "scheduled"]
    assert len(booked) == 1
    assert booked[0]["channel"] == "41.1", "the live channel, not the repeat"
    grab = db.one("SELECT * FROM our_grabs")
    assert grab["begins_at"] == fake_plex.LIVE_AT


def test_preview_mode_writes_nothing_to_plex(plex, synced):
    db.set_setting("dry_run", "1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 236, 'Kansas City Chiefs', 1, 0)")
    out = passes.run_passes()
    assert [d["action"] for d in out] == ["would schedule"]
    assert fake_plex.STATE.created == []
    assert not db.query("SELECT 1 FROM our_grabs")


def test_a_pass_limited_to_a_network_with_no_airing_says_which(plex, synced):
    with db.tx() as c:
        c.execute("""INSERT INTO passes (kind, team_id, team_name, networks, channels,
                                         enabled, created_at)
                     VALUES ('team', 236, 'Kansas City Chiefs', '["ABC","CBS"]', '[]', 1, 0)""")
    out = passes.run_passes()
    assert out[0]["action"] == "skipped"
    assert out[0]["reason"] == "no airing is on ABC or CBS"
    assert fake_plex.STATE.created == []


def test_a_pass_setting_the_one_shot_template_does_not_offer_is_dropped(plex, synced):
    """The live fault, 2026-09-10: 364 failed bookings and no Chiefs game.

    A pass created before the UI hid them kept `onlyNewAirings` in its stored
    settings. A pass always books the one-shot template, which does not offer
    that setting, so Plex answered 400 to every attempt for three weeks. The
    booking must survive a stored setting this template will not take.
    """
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test",
                     prefs={"onlyNewAirings": "1", "startOffsetMinutes": "1",
                            "autoDeletionItemPolicyWatchedLibrary": "0"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert "onlyNewAirings" not in made
    assert "autoDeletionItemPolicyWatchedLibrary" not in made
    assert made["startOffsetMinutes"] == "1", "a setting it does offer still applies"
    assert made["oneShot"] == "1", "the pin survives the filter"


def test_the_pin_survives_a_template_that_declares_nothing(plex, synced):
    """No Setting list means an unknown server, not an empty allowlist.

    Filtering against nothing would drop the three pinning settings, and the
    pin is the mechanism the whole app exists for.
    """
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    bare = dict(passes.single_template(passes.templates(plex, pick)))
    bare.pop("Setting", None)
    passes._schedule(plex, pick, None, "test", template=bare,
                     prefs={"startOffsetMinutes": "1"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["startOffsetMinutes"] == "1"


def _airing_with_teams(aid, guid, vcn, begins, teams, title, league):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  (guid, title, "rk", json.dumps(teams), league, "sports"))
        c.execute("INSERT OR IGNORE INTO channels (vcn, network, call_sign) VALUES (?,?,?)",
                  (vcn, "NBC", vcn))
        c.execute("""INSERT OR REPLACE INTO airings
                     (id, program_guid, channel_vcn, channel_identifier, begins_at,
                      ends_at, premiere, drm) VALUES (?,?,?,?,?,?,1,0)""",
                  (aid, guid, vcn, f"id-{vcn}", begins, begins + 3600))


def test_a_team_id_is_only_meaningful_inside_its_own_programme():
    """The live fault, 2026-09-10: a Kansas City Chiefs pass matched Borussia
    Dortmund, Arizona State, Washburn and a Chicago Bears game.

    A pass holds an id from Plex's section-level team list. A programme holds
    its own array, numbered per programme. Of the 89 ids that appeared in both
    on the live guide, 71 named a different team in each. Inside the programme
    arrays alone, id 343 was Kansas City Chiefs, Borussia Dortmund, Los Angeles
    Rams AND San Diego State. Matching on the number books whatever sport
    happens to share it.
    """
    soon = int(time.time()) + 3600
    _airing_with_teams("chiefs", "plex://episode/chiefs", "41.1", soon,
                       [{"id": 343, "name": "Kansas City Chiefs"},
                        {"id": 349, "name": "Denver Broncos"}],
                       "Denver Broncos at Kansas City Chiefs", "NFL Football")
    _airing_with_teams("bvb", "plex://episode/bvb", "38.1", soon + 60,
                       [{"id": 343, "name": "Borussia Dortmund"},
                        {"id": 344, "name": "SC Paderborn"}],
                       "Borussia Dortmund vs. Paderborn 07", "Bundesliga")

    got = passes.candidate_airings(343, team_name="Kansas City Chiefs")
    titles = {r["title"] for r in got}
    assert "Denver Broncos at Kansas City Chiefs" in titles
    assert "Borussia Dortmund vs. Paderborn 07" not in titles, \
        "a shared id is not a shared team"


def test_a_team_pass_still_matches_after_the_guide_renumbers_it():
    """The reason the id was in the match at all. The name has to carry it,
    because the id moves: one refresh took the Chiefs from 236 to 245 on the
    same game with the same guid."""
    soon = int(time.time()) + 3600
    _airing_with_teams("renum", "plex://episode/renum", "41.1", soon,
                       [{"id": 999, "name": "Kansas City Chiefs"},
                        {"id": 998, "name": "Denver Broncos"}],
                       "Denver Broncos at Kansas City Chiefs", "NFL Football")
    got = passes.candidate_airings(236, team_name="Kansas City Chiefs")
    assert [r["id"] for r in got] == ["renum"], "found by name, whatever the id"


def test_a_pass_with_no_name_at_all_still_falls_back_to_the_id():
    """An old pass made before the name was stored has nothing else to go on."""
    soon = int(time.time()) + 3600
    _airing_with_teams("byid", "plex://episode/byid", "41.1", soon,
                       [{"id": 4242, "name": "Kansas City Chiefs"}],
                       "Chiefs at Somebody", "NFL Football")
    assert [r["id"] for r in passes.candidate_airings(4242)] == ["byid"]


# ---- one airing per game, across channels ----

KC_BUF = [{"id": 159, "name": "Kansas City Chiefs"}, {"id": 337, "name": "Buffalo Bills"}]
NBC_GUID = "plex://episode/6a163c64ecdb6926550b22ad"
TELE_GUID = "plex://episode/6af24cdda0ee977335a83e21"
KICK = 1795742100


def _two_channel_game(teams2=None, gap=300):
    _airing_with_teams("nbc", NBC_GUID, "41.1", KICK, KC_BUF,
                       "Kansas City Chiefs at Buffalo Bills", "NFL Football")
    _airing_with_teams("tele", TELE_GUID, "39.1", KICK + gap, teams2 or KC_BUF,
                       "Kansas City Chiefs vs. Buffalo Bills", "Fútbol Americano NFL")


def _all():
    return db.query("SELECT a.*, p.title, p.teams, p.grandparent_title, p.rating_key, "
                    "p.summary, NULL channel_network FROM airings a JOIN programs p "
                    "ON p.guid = a.program_guid")


def test_two_guids_of_one_game_are_one_game():
    _two_channel_game()
    games = passes.group_by_game(_all())
    assert len(games) == 1
    only = next(iter(games.values()))
    assert {a["id"] for a in only} == {"nbc", "tele"}
    assert passes.game_guids(only) == sorted([NBC_GUID, TELE_GUID])


def test_team_order_does_not_matter():
    _two_channel_game(teams2=list(reversed(KC_BUF)))
    assert len(passes.group_by_game(_all())) == 1


def test_the_same_teams_a_week_apart_are_two_games():
    _two_channel_game(gap=7 * 86400)
    assert len(passes.group_by_game(_all())) == 2


def test_a_doubleheader_is_two_games():
    # Game 2 of a baseball doubleheader starts about three hours after game 1.
    _two_channel_game(gap=3 * 3600)
    assert len(passes.group_by_game(_all())) == 2


def test_different_teams_at_one_time_are_two_games():
    _two_channel_game(teams2=[{"id": 159, "name": "Kansas City Chiefs"},
                              {"id": 1, "name": "Denver Broncos"}])
    assert len(passes.group_by_game(_all())) == 2


def test_rows_with_no_teams_still_group_by_guid():
    _airing_with_teams("n1", "g1", "41.1", KICK, [], "Show", "Talk")
    _airing_with_teams("n2", "g2", "39.1", KICK + 60, [], "Show", "Talk")
    assert len(passes.group_by_game(_all())) == 2


def test_a_repeat_of_the_same_guid_stays_in_its_game():
    _two_channel_game()
    _airing_with_teams("nbc2", NBC_GUID, "41.1", KICK + 2 * 86400, KC_BUF,
                       "Kansas City Chiefs at Buffalo Bills", "NFL Football")
    games = passes.group_by_game(_all())
    assert len(games) == 1 and len(next(iter(games.values()))) == 3


def test_a_pass_books_one_airing_of_a_two_guid_game(plex, synced):
    now = int(time.time())
    kick = now + 3 * 3600
    _airing_with_teams("nbc", NBC_GUID, "41.1", kick, KC_BUF, "A at B", "NFL Football")
    _airing_with_teams("tele", TELE_GUID, "39.1", kick + 300, KC_BUF, "A vs. B", "Fútbol")
    db.set_setting("dry_run", "1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 159, 'Kansas City Chiefs', 1, 0)")
    out = [d for d in passes.run_passes() if d["game"] in ("A at B", "A vs. B")]
    assert len(out) == 1 and out[0]["action"] == "would schedule"


def test_a_booked_sibling_guid_makes_the_game_handled():
    _two_channel_game()
    with db.tx() as c:
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn,
                                            begins_at, source, subscription, created_at)
                     VALUES ('nbc',?,'G','41.1',?,'pass','7',?)""",
                  (NBC_GUID, KICK, int(time.time())))
    assert passes.already_handled(TELE_GUID) is None, "alone, the other guid is open"
    assert passes.already_handled([TELE_GUID, NBC_GUID]) == "already booked by a pass"


def _book(airing_id, guid, vcn, begins, pass_id, source="pass", sub="9"):
    with db.tx() as c:
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn,
                                            begins_at, source, subscription, pass_id,
                                            created_at)
                     VALUES (?,?,'G',?,?,?,?,?,0)""",
                  (airing_id, guid, vcn, begins, source, sub, pass_id))


def _pass():
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 159, 'Kansas City Chiefs', 1, 0)")
    return db.one("SELECT id FROM passes")["id"]


def test_cleanup_cancels_the_extra_pass_booking(plex):
    kick = int(time.time()) + 3 * 3600
    _airing_with_teams("nbc", NBC_GUID, "41.1", kick, KC_BUF, "A at B", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", kick + 300, KC_BUF, "A vs. B", "Fútbol")
    pid = _pass()
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})
    out = passes.dedupe_games()
    assert list(fake_plex.STATE.subscriptions) == ["1"], "Plex's grab is gone too"
    assert [d["action"] for d in out] == ["cancelled"], out
    assert [g["airing_id"] for g in db.query("SELECT airing_id FROM our_grabs")] == ["nbc"]
    log = db.one("SELECT * FROM pass_actions WHERE action = 'cancelled'")
    assert log["airing_id"] == "tele"
    assert log["reason"] == "another channel already records this game"
    assert passes.dedupe_games() == [], "a second run finds nothing"


def test_cleanup_leaves_a_manual_booking_alone(plex):
    kick = int(time.time()) + 3 * 3600
    _airing_with_teams("nbc", NBC_GUID, "41.1", kick, KC_BUF, "A at B", "NFL")
    _airing_with_teams("tele", TELE_GUID, "39.1", kick + 300, KC_BUF, "A vs. B", "Fútbol")
    pid = _pass()
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", kick + 300, None, source="manual", sub="2")
    assert passes.dedupe_games() == []
    assert db.one("SELECT COUNT(*) n FROM our_grabs")["n"] == 2


def test_cleanup_does_not_touch_two_games_of_one_pass(plex):
    kick = int(time.time()) + 3 * 3600
    _airing_with_teams("g1", NBC_GUID, "41.1", kick, KC_BUF, "A at B", "NFL")
    _airing_with_teams("g2", TELE_GUID, "41.1", kick + 7 * 86400, KC_BUF, "A at B", "NFL")
    pid = _pass()
    _book("g1", NBC_GUID, "41.1", kick, pid)
    _book("g2", TELE_GUID, "41.1", kick + 7 * 86400, pid)
    assert passes.dedupe_games() == []


# ---- channels per game: one, preferred language, any ----

_THE_GAME = {}


def _set_lang(vcn, detected=None, override=None):
    with db.tx() as c:
        c.execute("UPDATE channels SET language = ?, language_override = ? WHERE vcn = ?",
                  (detected, override, vcn))


def _bills_game(kick=None):
    """The real case: NBC 41.1 in English, Telemundo 39.1 in Spanish."""
    kick = kick or int(time.time()) + 3 * 3600
    _THE_GAME["kick"] = kick
    _airing_with_teams("nbc", NBC_GUID, "41.1", kick, KC_BUF,
                       "Kansas City Chiefs at Buffalo Bills", "NFL Football")
    _airing_with_teams("tele", TELE_GUID, "39.1", kick + 300, KC_BUF,
                       "Kansas City Chiefs vs. Buffalo Bills", "Fútbol Americano NFL")
    _set_lang("41.1", "en")
    _set_lang("39.1", "es")
    return kick


def _mode_pass(mode):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at, "
                  "channels_per_game) VALUES ('team', 159, 'Kansas City Chiefs', 1, 0, ?)",
                  (mode,))
    return db.one("SELECT id FROM passes ORDER BY id DESC")["id"]


def _dry_channels(span=600):
    db.set_setting("dry_run", "1")
    kick = _THE_GAME["kick"]     # the fake guide has games of its own
    out = [d for d in passes.run_passes()
           if d.get("action") == "would schedule"
           and kick - 600 <= d["begins_at"] <= kick + span]
    return sorted(d["channel"] for d in out)


def test_a_new_pass_is_one_airing_per_game_by_default():
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 159, 'Chiefs', 1, 0)")
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "one"


def test_the_migration_gives_an_existing_pass_one():
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 159, 'Chiefs', 1, 0)")
    conn = db.connect()
    conn.execute("ALTER TABLE passes DROP COLUMN channels_per_game")
    conn.commit()
    db._migrate(conn)
    conn.commit()
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "one"


def test_one_books_a_single_channel(plex, synced):
    _bills_game()
    _mode_pass("one")
    assert _dry_channels() == ["41.1"]


def test_any_books_both_channels(plex, synced):
    _bills_game()
    _mode_pass("any")
    assert _dry_channels() == ["39.1", "41.1"]


def test_preferred_english_books_only_the_english_channel(plex, synced):
    _bills_game()
    db.set_setting("preferred_language", "en")
    _mode_pass("preferred")
    assert _dry_channels() == ["41.1"]


def test_preferred_spanish_books_only_the_spanish_channel(plex, synced):
    _bills_game()
    db.set_setting("preferred_language", "es")
    _mode_pass("preferred")
    assert _dry_channels() == ["39.1"]


def test_preferred_books_every_channel_in_that_language(plex, synced):
    kick = _bills_game()
    _airing_with_teams("nbc2", "plex://episode/peacock", "42.1", kick + 120, KC_BUF,
                       "Chiefs at Bills (second feed)", "NFL Football")
    _set_lang("42.1", "en")
    db.set_setting("preferred_language", "en")
    _mode_pass("preferred")
    assert _dry_channels() == ["41.1", "42.1"]


def test_preferred_falls_back_to_one_airing_when_none_is_in_the_language(plex, synced):
    _bills_game()
    db.set_setting("preferred_language", "fr")
    _mode_pass("preferred")
    got = _dry_channels()
    assert got == ["41.1"], "the game is never lost, and it is one airing"
    log = db.one("SELECT reason FROM pass_actions WHERE action = 'would schedule' "
                 "AND begins_at = ?", (_THE_GAME["kick"],))
    assert "no channel carries it in French" in log["reason"]


def test_an_unplaced_channel_is_not_an_extra_channel_in_preferred(plex, synced):
    _bills_game()
    _set_lang("39.1", None)
    db.set_setting("preferred_language", "en")
    _mode_pass("preferred")
    assert _dry_channels() == ["41.1"], "an unknown channel is never booked as an extra"


def test_when_no_channel_is_placed_the_single_airing_fallback_still_books_one(plex, synced):
    _bills_game()
    _set_lang("41.1", None)
    _set_lang("39.1", None)
    _mode_pass("preferred")
    assert _dry_channels() == ["41.1"]


def test_preferred_does_not_swap_the_live_game_for_a_next_day_replay(plex, synced):
    kick = _bills_game()
    _set_lang("41.1", "es")           # the live broadcast is not in English
    _airing_with_teams("replay", NBC_GUID, "42.1", kick + 86400, KC_BUF,
                       "Kansas City Chiefs at Buffalo Bills", "NFL Football")
    _set_lang("42.1", "en")
    db.set_setting("preferred_language", "en")
    _mode_pass("preferred")
    got = _dry_channels()
    assert got == ["39.1"] or got == ["41.1"], "one live airing, not the replay"
    assert "42.1" not in got


def test_the_users_override_beats_the_detected_language(plex, synced):
    _bills_game()
    _set_lang("39.1", "es", override="en")
    db.set_setting("preferred_language", "en")
    _mode_pass("preferred")
    assert _dry_channels() == ["39.1", "41.1"]


def test_the_extra_modes_still_obey_a_channel_limit(plex, synced):
    _bills_game()
    pid = _mode_pass("any")
    with db.tx() as c:
        c.execute("UPDATE passes SET channels = ? WHERE id = ?", (json.dumps(["39.1"]), pid))
    assert _dry_channels() == ["39.1"]


def test_the_extra_modes_still_obey_a_network_limit(plex, synced):
    _bills_game()
    with db.tx() as c:
        c.execute("UPDATE channels SET network = 'Telemundo' WHERE vcn = '39.1'")
    pid = _mode_pass("any")
    with db.tx() as c:
        c.execute("UPDATE passes SET networks = ? WHERE id = ?", (json.dumps(["NBC"]), pid))
    assert _dry_channels() == ["41.1"]


def test_any_skips_a_drm_channel(plex, synced):
    _bills_game()
    with db.tx() as c:
        c.execute("UPDATE airings SET drm = 1 WHERE id = 'tele'")
    _mode_pass("any")
    assert _dry_channels() == ["41.1"]


def test_a_replay_on_another_channel_the_next_day_is_not_booked(plex, synced):
    kick = _bills_game()
    _airing_with_teams("replay", NBC_GUID, "42.1", kick + 86400, KC_BUF,
                       "Kansas City Chiefs at Buffalo Bills", "NFL Football")
    _set_lang("42.1", "en")
    _mode_pass("any")
    assert _dry_channels() == ["39.1", "41.1"]


def test_a_booked_nbc_airing_does_not_block_telemundo_when_the_mode_wants_both():
    _bills_game()
    _book("nbc", NBC_GUID, "41.1", KICK, None, sub="1")
    guids = [NBC_GUID, TELE_GUID]
    assert passes.already_handled(guids) == "already booked by a pass", "per game, as before"
    assert passes.already_handled(guids, channel="39.1") is None
    assert passes.already_handled(guids, channel="41.1") == "already booked by a pass"


def test_per_channel_handling_sees_our_own_recorder_and_plex(plex):
    kick = _bills_game()
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, channel_vcn, begins_at, status) "
                  "VALUES ('g1', '41.1', ?, 'scheduled')", (kick,))
    guids = [NBC_GUID, TELE_GUID]
    assert passes.already_handled(guids, channel="41.1").startswith("Plex already has it")
    assert passes.already_handled(guids, channel="39.1") is None


def test_the_second_run_books_only_the_missing_channel(plex, synced):
    kick = _bills_game()
    pid = _mode_pass("any")
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    with db.tx() as c:      # Plex lists the booking, so it is real
        c.execute("INSERT INTO plex_subscriptions (key) VALUES ('1')")
    assert _dry_channels() == ["39.1"]


def _booked_both(pid):
    kick = _bills_game()
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})


def test_dedupe_in_one_cancels_the_extra_channel(plex):
    pid = _mode_pass("one")
    _booked_both(pid)
    passes.dedupe_games()
    assert list(fake_plex.STATE.subscriptions) == ["1"]


def test_dedupe_in_any_cancels_nothing(plex):
    pid = _mode_pass("any")
    _booked_both(pid)
    assert passes.dedupe_games() == []
    assert len(fake_plex.STATE.subscriptions) == 2


def test_dedupe_in_preferred_cancels_only_the_other_language(plex):
    pid = _mode_pass("preferred")
    _booked_both(pid)
    db.set_setting("preferred_language", "en")
    out = passes.dedupe_games()
    assert [d["action"] for d in out] == ["cancelled"]
    assert list(fake_plex.STATE.subscriptions) == ["1"]
    log = db.one("SELECT * FROM pass_actions WHERE action = 'cancelled'")
    assert log["airing_id"] == "tele"
    assert "preferred language" in log["reason"]


def test_dedupe_in_preferred_keeps_at_least_one_booking(plex):
    pid = _mode_pass("preferred")
    _booked_both(pid)
    db.set_setting("preferred_language", "fr")
    passes.dedupe_games()
    assert list(fake_plex.STATE.subscriptions) == ["1"], "the pass's own first choice stays"
    assert db.one("SELECT airing_id FROM pass_actions WHERE action = 'cancelled'")["airing_id"] \
        == "tele"


def test_changing_back_to_one_lets_the_next_run_cancel_the_extra(plex):
    pid = _mode_pass("any")
    _booked_both(pid)
    assert passes.dedupe_games() == []
    with db.tx() as c:
        c.execute("UPDATE passes SET channels_per_game = 'one' WHERE id = ?", (pid,))
    out = passes.dedupe_games()
    assert [d["action"] for d in out] == ["cancelled"]
    assert db.one("SELECT reason FROM pass_actions WHERE action = 'cancelled'")["reason"] \
        == "another channel already records this game"


def test_the_preview_counts_and_peak_overlap(plex, synced):
    _bills_game()
    kick = _THE_GAME["kick"]
    rows = [r for r in passes.candidate_airings(159, team_name="Kansas City Chiefs")
            if kick <= r["begins_at"] <= kick + 600]
    one = passes.plan(rows, [], [], "one", "en")
    both = passes.plan(rows, [], [], "any", "en")
    assert len(one) == 1 and len(both) == 2
    assert passes.peak_overlap(one) == 1 and passes.peak_overlap(both) == 2


def test_any_collapses_two_bookings_on_one_channel(plex):
    pid = _mode_pass("any")
    kick = _bills_game()
    _airing_with_teams("nbc_b", NBC_GUID, "41.1", kick + 600, KC_BUF, "A at B", "NFL")
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("nbc_b", NBC_GUID, "41.1", kick + 600, pid, sub="2")
    _book("tele", TELE_GUID, "39.1", kick + 300, pid, sub="3")
    fake_plex.STATE.subscriptions.update({k: {"key": k} for k in "123"})
    out = passes.dedupe_games()
    assert [d["action"] for d in out] == ["cancelled"]
    assert sorted(fake_plex.STATE.subscriptions) == ["1", "3"]


def test_per_channel_handling_sees_our_own_recorder(plex):
    kick = _bills_game()
    with db.tx() as c:
        c.execute("INSERT INTO recordings (uid, kind, state, program_guid, channel_vcn, "
                  "begins_at, starts_at, source) VALUES "
                  "('u-rec','recording','scheduled',?,'41.1',?,?,'pass')", (NBC_GUID, kick, kick))
    guids = [NBC_GUID, TELE_GUID]
    assert passes.already_handled(guids, channel="41.1") == "CouchElephant is recording it"
    assert passes.already_handled(guids, channel="39.1") is None
    assert passes.already_handled(guids) == "CouchElephant is recording it"


def _run_and_count():
    out = passes.run_passes()
    return ([d for d in out if d["action"] == "scheduled"],
            [d for d in out if d["action"] == "cancelled"])


def test_a_second_real_run_books_and_cancels_nothing(plex, synced):
    for mode in ("preferred", "any"):
        with db.tx() as c:
            c.execute("DELETE FROM passes")
            c.execute("DELETE FROM our_grabs")
        fake_plex.STATE.subscriptions.clear()
        with db.tx() as c:
            c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at, "
                      "channels_per_game) VALUES ('team', 236, 'Kansas City Chiefs', 1, 0, ?)",
                      (mode,))
        first, _ = _run_and_count()
        assert len(first) >= 1, mode
        second, gone = _run_and_count()
        assert second == [] and gone == [], mode


def test_changing_a_booked_pass_from_preferred_to_one_cancels_nothing_it_needs(plex, synced):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at, "
                  "channels_per_game) VALUES ('team', 236, 'Kansas City Chiefs', 1, 0, 'preferred')")
    first, _ = _run_and_count()
    with db.tx() as c:
        c.execute("UPDATE passes SET channels_per_game = 'one'")
    again, gone = _run_and_count()
    assert again == [] and gone == []
    assert len(db.query("SELECT 1 FROM our_grabs")) == len(first)


# ---- language decides ties ----

def _telemundo_first():
    """Telemundo is the premiere and five minutes earlier than NBC."""
    kick = _bills_game()
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'nbc'")
        c.execute("UPDATE airings SET begins_at = ?, ends_at = ? WHERE id = 'tele'",
                  (kick - 300, kick + 3300))
    return kick


def test_one_airing_per_game_prefers_the_preferred_language(plex, synced):
    _telemundo_first()
    db.set_setting("preferred_language", "en")
    _mode_pass("one")
    assert _dry_channels() == ["41.1"], "English first, even though Telemundo is live"


def test_one_airing_still_takes_the_other_language_when_it_is_the_only_one(plex, synced):
    _telemundo_first()
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE id = 'nbc'")
    db.set_setting("preferred_language", "en")
    _mode_pass("one")
    assert _dry_channels() == ["39.1"]


def test_unknown_ranks_between_preferred_and_other(plex, synced):
    _telemundo_first()
    _set_lang("41.1", None)
    db.set_setting("preferred_language", "en")
    _mode_pass("one")
    assert _dry_channels() == ["41.1"], "unknown beats a known other language"


def _booked_both_with(pid, tele_first):
    kick = _bills_game()
    with db.tx() as c:
        if tele_first:
            c.execute("UPDATE airings SET premiere = 0 WHERE id = 'nbc'")
            c.execute("UPDATE airings SET begins_at = ? WHERE id = 'tele'", (kick - 300,))
    _book("nbc", NBC_GUID, "41.1", kick, pid, sub="1")
    _book("tele", TELE_GUID, "39.1", kick - 300 if tele_first else kick + 300, pid, sub="2")
    fake_plex.STATE.subscriptions.update({"1": {"key": "1"}, "2": {"key": "2"}})


def test_switching_to_one_keeps_the_english_booking_even_if_telemundo_is_live(plex):
    pid = _mode_pass("any")
    _booked_both_with(pid, tele_first=True)
    with db.tx() as c:
        c.execute("UPDATE passes SET channels_per_game = 'one'")
    passes.dedupe_games()
    assert list(fake_plex.STATE.subscriptions) == ["1"]


def test_switching_to_one_keeps_english_when_telemundo_is_the_premiere(plex):
    pid = _mode_pass("any")
    _booked_both_with(pid, tele_first=False)
    with db.tx() as c:
        c.execute("UPDATE airings SET premiere = 0 WHERE id = 'nbc'")
        c.execute("UPDATE passes SET channels_per_game = 'one'")
    passes.dedupe_games()
    assert list(fake_plex.STATE.subscriptions) == ["1"]
