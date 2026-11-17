"""Sports overtime: a game that runs long keeps recording while the scoreboard
says it is still being played, and never ends sooner than its padding."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, overtime, recorder
from app.web import app
from tests.test_recorder import _airing


def _team(display, location, name, short=None):
    return {"team": {"displayName": display, "location": location, "name": name,
                     "shortDisplayName": short or name}}


def _event(eid, begins, state, home, away, completed=False):
    return {"id": eid, "name": "game", "date": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(begins)),
            "status": {"type": {"state": state, "completed": completed}},
            "competitions": [{"competitors": [home, away]}]}


CHIEFS = _team("Kansas City Chiefs", "Kansas City", "Chiefs")
BRONCOS = _team("Denver Broncos", "Denver", "Broncos")


class _Board(list):
    asked: list


@pytest.fixture
def board(monkeypatch):
    """The scoreboard, as a list the test changes. Every league answers with it."""
    events = _Board()
    asked = []

    def fake(path, query, day):
        asked.append(path)
        if events and events[0] == "down":
            raise httpx.ConnectError("no route")
        return events
    monkeypatch.setattr(overtime, "_get", fake)
    events.asked = asked
    return events


def _game(begins, length=3 * 3600, title="Chiefs at Broncos", show="NFL Football"):
    """A sports recording of ours, recording now, with 30 minutes of padding."""
    rec_id = recorder.book(_airing("a1", "g1", "5.1", begins, length=length, title=title,
                                   show=show))
    rec = recorder.get(rec_id)
    recorder._set(rec_id, state="recording", started_at=begins,
                  stops_at=rec["ends_at"] + 1800)
    return recorder.get(rec_id)


def test_a_game_still_being_played_keeps_recording_and_stops_after_the_final(board):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "in", BRONCOS, CHIEFS))
    base = rec["stops_at"]

    overtime.tick(rec["ends_at"] - 3600)
    rec = recorder.get(rec["id"])
    assert rec["ot_event"] == "football/nfl/401"
    assert rec["ot_base"] == base

    # Near the end of the padding the game is still on: the stop moves on
    # before the recorder reaches the old one.
    at = base - 120
    overtime.tick(at)
    rec = recorder.get(rec["id"])
    assert rec["stops_at"] == at + overtime.STILL_PLAYING_SECONDS > base
    assert rec["ot_state"] == "in"
    assert "ran long" in rec["ot_note"]

    # Final: five minutes after, and no more asking.
    board[0] = _event("401", begins, "post", BRONCOS, CHIEFS, completed=True)
    at += overtime.POLL_SECONDS
    overtime.tick(at)
    rec = recorder.get(rec["id"])
    assert rec["stops_at"] == at + overtime.AFTER_FINAL_SECONDS
    assert rec["ot_state"] == "post"


def test_a_game_that_ends_on_time_keeps_its_padding(board):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "post", BRONCOS, CHIEFS, completed=True))
    overtime.tick(rec["ends_at"] - 3600)
    overtime.tick(rec["ends_at"] - 60)
    assert recorder.get(rec["id"])["stops_at"] == rec["stops_at"]


def test_never_past_the_cap(board):
    db.set_setting("overtime_cap_minutes", "60")
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "in", BRONCOS, CHIEFS))
    overtime.tick(rec["ends_at"] - 3600)
    overtime.tick(rec["ends_at"] + 3550)
    assert recorder.get(rec["id"])["stops_at"] == rec["ends_at"] + 3600


def test_a_game_not_on_the_scoreboard_keeps_its_padding(board):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "in", _team("Las Vegas Raiders", "Las Vegas", "Raiders"),
                        _team("Dallas Cowboys", "Dallas", "Cowboys")))
    overtime.tick(rec["ends_at"] - 3600)
    overtime.tick(rec["ends_at"] + 1800)
    rec2 = recorder.get(rec["id"])
    assert rec2["ot_state"] == "nomatch"
    assert rec2["stops_at"] == rec["stops_at"]


def test_a_scoreboard_that_does_not_answer_changes_nothing(board):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append("down")
    overtime.tick(rec["ends_at"] - 3600)
    assert recorder.get(rec["id"])["stops_at"] == rec["stops_at"]
    assert recorder.get(rec["id"])["ot_state"] == "unreachable"


def test_turned_off_does_nothing(board):
    db.set_setting("sports_overtime", "0")
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "in", BRONCOS, CHIEFS))
    overtime.tick(rec["stops_at"] + 60)
    assert recorder.get(rec["id"])["stops_at"] == rec["stops_at"]
    assert not board.asked


def test_a_recording_that_needs_the_tuner_ends_the_overtime(board, tuner):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    board.append(_event("401", begins, "in", BRONCOS, CHIEFS))
    overtime.tick(rec["ends_at"] - 3600)
    at = rec["stops_at"] + 60
    overtime.tick(at)
    # Another recording is due and every tuner is busy.
    other = recorder.book(_airing("a2", "g2", "9.1", at - 60, title="News", show="News"))
    recorder._set(other, starts_at=at - 60)
    tuner.plex_holds = ["4.1", "9.1", "38.1", "41.1"]
    overtime.tick(at + 30)
    rec = recorder.get(rec["id"])
    assert rec["stops_at"] == at + 30
    assert "needed the tuner" in rec["ot_note"]


def test_the_header_names_the_game_during_overtime(board):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    # The guide has the next programme on from the game's scheduled end.
    _airing("a3", "g3", "5.1", rec["ends_at"], length=3600, title="Evening News",
            show="Evening News")
    board.append(_event("401", begins, "in", BRONCOS, CHIEFS))
    overtime.tick(rec["ends_at"] - 3600)
    overtime.tick(rec["stops_at"] + 60)
    seconds = rec["ends_at"] + 2400 - begins
    got = TestClient(app).get(f"/api/tv/recordings/{rec['id']}/now?seconds={seconds}").json()
    assert "Chiefs" in (got["program"]["title"] + got["program"]["subtitle"])


@pytest.mark.parametrize("guide,espn", [
    ("Ohio St.", _team("Ohio State Buckeyes", "Ohio State", "Buckeyes")),
    ("Air Force", _team("Air Force Falcons", "Air Force", "Falcons")),
    ("Kansas City Chiefs", CHIEFS),
    ("Club América", _team("Club América", "América", "América")),
])
def test_team_names_match_across_the_guide_and_the_scoreboard(guide, espn):
    assert overtime._same_team(guide, overtime._names(espn))


def test_the_guide_title_picks_the_league():
    def leagues(show, title="A at B"):
        return [p for p, _ in overtime.leagues_for({"show_title": show, "title": title})]
    assert leagues("Fútbol Americano NFL") == ["football/nfl"]
    assert leagues("Women's College Basketball") == ["basketball/womens-college-basketball"]
    assert leagues("College Basketball") == ["basketball/mens-college-basketball"]
    assert len(leagues("Sports Special")) == len(overtime.LEAGUES)


def test_the_settings_save_from_both_places():
    c = TestClient(app)
    c.post("/settings/recorder", data={"sports_overtime": ["0", "1"],
                                       "overtime_cap_minutes": "999"}, follow_redirects=False)
    assert db.get_setting("sports_overtime") == "1"
    assert db.get_setting("overtime_cap_minutes") == "360"
    assert c.post("/api/tv/settings", data={"sports_overtime": "0"}).json()["ok"]
    assert not overtime.enabled()


def test_one_team_and_the_start_time_are_enough_when_only_one_game_fits(board):
    """The guide says "Texas-El Paso" where the scoreboard says "UTEP"."""
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins, title="Texas-El Paso at Air Force", show="College Football")
    board.append(_event("77", begins + 600, "in", _team("Air Force Falcons", "Air Force", "Falcons"),
                        _team("UTEP Miners", "UTEP", "Miners")))
    ref, said = overtime.find(rec)
    assert ref == "football/college-football/77"
    # Two games that could fit is not a match.
    board.append(_event("78", begins, "in", _team("Air Force Falcons", "Air Force", "Falcons"),
                        _team("Navy Midshipmen", "Navy", "Midshipmen")))
    assert overtime.find(rec)[0] is None


def test_the_recorder_itself_keeps_recording_through_overtime(board, tuner):
    """End to end with the real recorder and the fake tuner: a moved stop time
    is what the capture obeys, not the one it was booked with."""
    from tests.test_recorder import _until
    now = int(time.time())
    rec_id = recorder.book(_airing("a1", "g1", "5.1", now - 60, length=600))
    _until(lambda: (recorder.tick() or recorder.get(rec_id)["state"] == "recording"))
    rec = recorder.get(rec_id)
    board.append(_event("401", rec["begins_at"], "in", BRONCOS, CHIEFS))
    overtime.tick(now)
    base = recorder.get(rec_id)["stops_at"]
    # Past the padding, the game is still on: extended.
    overtime.tick(base - 30)
    extended = recorder.get(rec_id)["stops_at"]
    assert extended > base
    # The recorder's own clock passes the old stop: it keeps recording.
    recorder.tick(now=base + 1)
    assert recorder.get(rec_id)["state"] == "recording"
    assert recorder.busy(rec_id)
    # And stops at the new one.
    recorder.tick(now=extended + 1)
    assert recorder.get(rec_id)["state"] == "finished"


def test_one_league_not_answering_is_not_no_game(board, monkeypatch):
    """The right league may be the one that failed: try again, do not say
    there is no game."""
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins, show="Sports Special")

    def flaky(path, query, day):
        if path == "football/nfl":
            raise httpx.ReadTimeout("slow")
        return []
    monkeypatch.setattr(overtime, "_get", flaky)
    overtime.tick(rec["ends_at"] - 3600)
    assert recorder.get(rec["id"])["ot_state"] == "unreachable"
    # Still unanswered after the game's end: too late, the padding stands.
    overtime.tick(rec["ends_at"] + 60)
    assert recorder.get(rec["id"])["ot_state"] == "nomatch"
    assert recorder.get(rec["id"])["stops_at"] == rec["stops_at"]


def test_a_broken_answer_is_caught_and_changes_nothing(board, monkeypatch):
    begins = int(time.time()) - 3 * 3600
    rec = _game(begins)
    monkeypatch.setattr(overtime, "_get", lambda *a: [{"no": "id"}, None])
    overtime.tick(rec["ends_at"] - 3600)
    assert recorder.get(rec["id"])["stops_at"] == rec["stops_at"]
