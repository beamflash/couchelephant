"""Library folders suggested from Plex's own library locations."""
import os

import pytest

from app import db, handover
from app.routes import settings as routes
from tests import fake_plex

URL = "/api/recorder/library-suggest"


def _sections(tv, movies):
    fake_plex.STATE.sections = [
        {"key": "2", "type": "show", "title": "TV Shows",
         "Location": [{"id": 2, "path": tv}]},
        {"key": "1", "type": "movie", "title": "Movies",
         "Location": [{"id": 1, "path": movies}]},
        {"key": "3", "type": "artist", "title": "Music",
         "Location": [{"id": 3, "path": "/music"}]},
    ]


@pytest.fixture(autouse=True)
def _reset():
    yield
    fake_plex.STATE.sections = None


@pytest.fixture
def native(plex_url, monkeypatch):
    monkeypatch.setattr(handover, "LIB_DIR", None)
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "test-token")


def test_a_real_folder_is_writable_and_a_missing_one_is_not(client, native, tmp_path):
    tv = tmp_path / "TV"
    tv.mkdir()
    _sections(str(tv), str(tmp_path / "Movies"))
    j = client.get(URL).json()
    assert j["ok"] is True
    assert j["suggestions"] == [
        {"kind": "shows", "section_id": "2", "section": "TV Shows",
         "path": str(tv), "writable": True, "why": ""},
        {"kind": "movies", "section_id": "1", "section": "Movies",
         "path": str(tmp_path / "Movies"), "writable": False,
         "why": "That folder does not exist on this computer."}]


def test_the_check_leaves_no_file_behind(client, native, tmp_path):
    tv = tmp_path / "TV"
    tv.mkdir()
    _sections(str(tv), str(tmp_path / "none"))
    client.get(URL)
    assert os.listdir(tv) == []


def test_a_folder_that_cannot_be_written_says_so(client, native, tmp_path):
    tv = tmp_path / "TV"
    tv.mkdir()
    tv.chmod(0o555)
    try:
        if os.access(tv, os.W_OK):
            pytest.skip("running as a user who can write anywhere")
        _sections(str(tv), str(tmp_path / "none"))
        first = client.get(URL).json()["suggestions"][0]
        assert first["writable"] is False
        assert first["why"] == "CouchElephant cannot write to that folder."
    finally:
        tv.chmod(0o755)


def test_a_failed_plex_answers_plainly(client, native, monkeypatch):
    db.set_setting("plex_url", "http://127.0.0.1:1")
    j = client.get(URL).json()
    assert j["ok"] is False and "Plex" in j["error"]


def test_in_a_container_the_path_is_shown_inside_the_mount(client, plex_url, tmp_path, monkeypatch):
    lib = tmp_path / "library"
    (lib / "media" / "TV").mkdir(parents=True)
    (lib / "Movies").mkdir()
    monkeypatch.setattr(handover, "LIB_DIR", str(lib))
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "test-token")
    _sections("/data/media/TV", "/data/Movies")
    s = client.get(URL).json()["suggestions"]
    assert [(x["kind"], x["path"], x["writable"]) for x in s] == [
        ("shows", "media/TV", True), ("movies", "Movies", True)]


def test_in_a_container_a_path_outside_the_mount_is_not_writable(client, plex_url, tmp_path, monkeypatch):
    lib = tmp_path / "library"
    lib.mkdir()
    monkeypatch.setattr(handover, "LIB_DIR", str(lib))
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "test-token")
    _sections("/data/TV", "/data/Movies")
    s = client.get(URL).json()["suggestions"]
    assert all(not x["writable"] for x in s)
    assert s[0]["why"] == "That folder does not exist on this computer."


def test_saving_keeps_an_absolute_folder_on_a_native_install(native):
    routes.apply({"recorder_shows_folder": "/srv/plex/TV"})
    assert db.get_setting("recorder_shows_folder") == "/srv/plex/TV"
    routes.apply({"recorder_shows_folder": "/srv/../etc/TV"})
    assert db.get_setting("recorder_shows_folder") == "/srv/etc/TV"


def test_saving_in_a_container_still_makes_the_folder_relative(monkeypatch):
    monkeypatch.setattr(handover, "LIB_DIR", "/library")
    routes.apply({"recorder_shows_folder": "/TV/../x"})
    assert db.get_setting("recorder_shows_folder") == "TV/x"
