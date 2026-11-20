"""Settings, Recording: library folders suggested from Plex."""
import pytest

from app import db, handover
from tests import fake_plex


@pytest.fixture
def folders(page, monkeypatch, tmp_path):
    tv = tmp_path / "TV"
    tv.mkdir()
    monkeypatch.setattr(handover, "LIB_DIR", None)
    fake_plex.STATE.sections = [
        {"key": "2", "type": "show", "title": "TV Shows",
         "Location": [{"id": 2, "path": str(tv)}]},
        {"key": "1", "type": "movie", "title": "Movies",
         "Location": [{"id": 1, "path": str(tmp_path / "Movies")}]},
    ]
    for k in ("recorder_shows_folder", "recorder_shows_section",
              "recorder_movies_folder", "recorder_movies_section"):
        db.set_setting(k, "")
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    page.click('.nav-item[data-sec="recording"]')
    page.click('#settabs .tab:has-text("CouchElephant DVR")')
    yield page, tv
    fake_plex.STATE.sections = None


def test_suggestions_show_and_nothing_is_saved_until_the_button(folders):
    page, tv = folders
    page.wait_for_selector("#recsuggest:not([hidden])")
    text = page.locator("#recsuglist").inner_text()
    assert str(tv) in text and "TV Shows" in text
    assert "That folder does not exist on this computer." in text
    assert page.locator("#recsugbtn").inner_text() == "Use these folders"
    assert db.get_setting("recorder_shows_folder") == ""
    assert db.get_setting("recorder_shows_section") == ""


def test_the_button_posts_the_recorder_form_with_folders_and_sections(folders):
    page, tv = folders
    page.wait_for_selector("#recsuggest:not([hidden])")
    page.click("#recsugbtn")
    page.wait_for_load_state()
    for _ in range(50):
        if db.get_setting("recorder_shows_folder"):
            break
        page.wait_for_timeout(100)
    assert db.get_setting("recorder_shows_folder") == str(tv)
    assert db.get_setting("recorder_shows_section") == "2"
    # The Movies folder is missing, so it is left alone.
    assert db.get_setting("recorder_movies_folder") == ""
