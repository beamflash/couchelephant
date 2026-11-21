"""Handing over to a show Plex already has: the file joins an old episode as
another Part, and the guide's episode number can be one off Plex's."""
import os

from app import handover, recorder
from tests import fake_plex
from tests.test_recorder import _record, library  # noqa: F401, F811


def _plex_has_murder(library_dir):
    """Plex's copy of the show: folder "Season 1", Sudden Death is E17."""
    season = os.path.join(library_dir, "Murder, She Wrote (1984)", "Season 1")
    os.makedirs(season)
    base = "/media/video/tv/Murder, She Wrote (1984)/Season 1/"
    fake_plex.STATE.shows["900"] = {"title": "Murder, She Wrote", "eps": [
        {"ratingKey": "9017", "title": "Sudden Death", "parentIndex": 1, "index": 17,
         "files": [base + "Murder.S01E17.mkv"]},
        {"ratingKey": "9018", "title": "Footnote to Murder", "parentIndex": 1, "index": 18,
         "files": [base + "Murder.S01E18.mkv"]},
    ]}
    return season


def _as_murder(rec, episode):
    recorder._set(rec["id"], show_title="Murder, She Wrote", title="Sudden Death",
                  ptype="episode", season=1, episode=episode)
    return recorder.get(rec["id"])


def test_a_file_that_becomes_a_second_part_of_an_old_episode_is_found(tuner, library):  # noqa: F811
    _plex_has_murder(library)
    rec = _as_murder(_record(), 17)
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex", done["error"]
    assert done["plex_rating_key"] == "9017"


def test_the_episode_is_numbered_as_plex_numbers_it(tuner, library):  # noqa: F811
    season = _plex_has_murder(library)
    rec = _as_murder(_record(), 18)         # the guide is one off
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex", done["error"]
    assert done["plex_rating_key"] == "9017", "not Footnote to Murder"
    assert done["plex_path"] == os.path.join(
        season, "Murder, She Wrote (1984) - S01E17 - Sudden Death.ts")
    assert not os.path.exists(os.path.join(library, "Murder, She Wrote (1984)", "Season 01"))


def test_a_file_put_under_the_wrong_number_is_moved_and_scanned(tuner, library):  # noqa: F811
    season = _plex_has_murder(library)
    rec = _as_murder(_record(), 18)
    wrong_dir = os.path.join(library, "Murder, She Wrote (1984)", "Season 01")
    os.makedirs(wrong_dir)
    wrong = os.path.join(wrong_dir, "Murder, She Wrote (1984) - S01E18 - Sudden Death.ts")
    with open(wrong, "wb") as f:
        f.write(b"x" * 10)
    other = os.path.join(season, "Murder.S01E18.mkv")
    with open(other, "wb") as f:
        f.write(b"theirs")
    recorder._set(rec["id"], plex_path=wrong, state="moving")
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex", done["error"]
    assert not os.path.exists(wrong)
    assert os.path.exists(done["plex_path"]) and "Season 1" in done["plex_path"]
    assert open(other, "rb").read() == b"theirs", "only our own file is touched"
    assert done["plex_rating_key"] == "9017"


def test_a_show_plex_does_not_have_keeps_the_guide_number(tuner, library):  # noqa: F811
    rec = _as_murder(_record(), 18)
    handover.tick(wait=True)
    done = recorder.get(rec["id"])
    assert done["state"] == "in_plex", done["error"]
    assert "S01E18" in done["plex_path"]


def test_an_episode_title_two_episodes_share_is_not_guessed(tuner, library):  # noqa: F811
    _plex_has_murder(library)
    fake_plex.STATE.shows["900"]["eps"].append(
        {"ratingKey": "9099", "title": "Sudden Death", "parentIndex": 2, "index": 3, "files": []})
    rec = _as_murder(_record(), 18)
    handover.tick(wait=True)
    assert "S01E18" in recorder.get(rec["id"])["plex_path"]
