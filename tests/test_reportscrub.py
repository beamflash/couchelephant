import json
import pathlib

import pytest

from app import db
from app.reportscrub import ADDRESS_SETTINGS, NEVER_SENT, SETTINGS_ALLOW, Scrubber

CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "data" / "scrub_corpus.json").read_text())


@pytest.mark.parametrize("case", CORPUS["secrets"], ids=lambda c: c["name"])
def test_corpus_no_secret_survives(case):
    # "known" cases have no shape to match; the caller supplies the value.
    known = [case["secret"]] if case.get("known") else []
    out = Scrubber(known_secrets=known).text(case["input"])
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


def test_known_secret_removed_even_without_shape():
    s = Scrubber(known_secrets=["abcdefghijklmnop", "abcdefghijklmnopqrstuv"])
    out = s.text("tok abcdefghijklmnopqrstuv and abcdefghijklmnop")
    assert "abcdefgh" not in out


def test_known_secret_ignores_tiny_values():
    assert Scrubber(known_secrets=["a", ""]).text("a cat") == "a cat"


def test_known_host_hashed_anywhere():
    s = Scrubber(known_hosts=["https://media.example.net:8443"])
    out = s.text("lookup media.example.net failed, MEDIA.example.net again")
    assert "example.net" not in out.lower()
    assert s.host("media.example.net") in out


def test_known_host_matches_address_hash():
    s = Scrubber(known_hosts=["media.example.net"])
    assert s.host("media.example.net") in s.address("https://media.example.net")


@pytest.mark.parametrize("raw", ["http://[bad", "http://[::1", "http://h:99999"])
def test_malformed_url_never_raises_or_leaks(raw):
    s = Scrubber()
    out = s.address(raw)
    assert out.startswith("host-") and raw not in out
    assert "bad" not in s.text("x " + raw + " y")


def test_ipv6_private_keeps_shape_public_hashed():
    s = Scrubber()
    assert s.text("at [fd00::1234]:5004") == "at private-lan:5004"
    assert s.text("at fe80::1%eth0 ok") == "at private-lan ok"
    assert "host-" in s.text("at 2001:db8::77 ok")


def test_pathological_input_is_fast():
    import time
    s = Scrubber()
    for unit in ("a.", "eyJ.", "-----BEGIN PRIVATE KEY-----", "a:", "token", "1."):
        big = unit * (200_000 // len(unit))
        t = time.perf_counter()
        s.text(big)
        assert time.perf_counter() - t < 1.0, unit
