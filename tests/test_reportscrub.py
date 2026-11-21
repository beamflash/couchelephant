import json
import pathlib

import pytest

from app import db
from app.reportscrub import ADDRESS_SETTINGS, NEVER_SENT, SETTINGS_ALLOW, Scrubber

CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "data" / "scrub_corpus.json").read_text())


@pytest.mark.parametrize("case", CORPUS["secrets"], ids=lambda c: c["name"])
def test_corpus_no_secret_survives(case):
    out = Scrubber().text(case["input"])
    assert case["secret"] not in out


@pytest.mark.parametrize("s", CORPUS["must_survive"])
def test_corpus_normal_text_survives(s):
    assert Scrubber().text(s) == s


def test_settings_only_allow_list():
    got = Scrubber().settings({
        "plex_token": "SECRET", "tmdb_key": "SECRET", "sportsdb_key": "SECRET",
        "new_setting": "SECRET", "timezone": "UTC", "dry_run": "1",
        "plex_url": "http://192.168.1.20:32400",
    })
    assert set(got) <= set(SETTINGS_ALLOW) | set(ADDRESS_SETTINGS)
    assert "SECRET" not in json.dumps(got)
    assert got["timezone"] == "UTC"
    assert got["plex_url"] == "http://private-lan:32400"
    assert "new_setting" not in got


def test_every_default_setting_is_classified():
    known = set(SETTINGS_ALLOW) | set(ADDRESS_SETTINGS) | set(NEVER_SENT)
    assert set(db.DEFAULTS) <= known, set(db.DEFAULTS) - known


def test_private_address_keeps_shape():
    s = Scrubber()
    assert s.address("http://192.168.1.20:32400") == "http://private-lan:32400"
    assert s.address("https://10.1.2.3") == "https://private-lan"
    assert s.address("http://localhost:8080") == "http://private-lan:8080"


def test_public_address_hashed():
    out = Scrubber().address("https://example.net:8443")
    assert "host-" in out and out.endswith(":8443")
    assert "example.net" not in out


def test_salt_differs_per_scrubber():
    assert Scrubber().host("example.net") != Scrubber().host("example.net")


def test_same_scrubber_hash_is_stable():
    s = Scrubber()
    assert s.host("example.net") == s.host("example.net")
    assert len(s.host("example.net")) == len("host-") + 10


def test_userinfo_dropped():
    out = Scrubber().address("https://user:hunter2@example.net/x")
    assert "hunter2" not in out and "user" not in out
    assert out.startswith("https://host-")
