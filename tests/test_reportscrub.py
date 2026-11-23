import json
import pathlib

import pytest

from app import backingstore, db, dbstore
from app.reportscrub import (
    ADDRESS_SETTINGS,
    NEVER_SENT,
    SECRET_SETTINGS_ALL,
    SETTINGS_ALLOW,
    Scrubber,
)

CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "data" / "scrub_corpus.json").read_text(
        encoding="utf-8"))
# The Swift suite uses the same salt, so the outputs can match byte for byte.
CORPUS_SALT = b"\x07" * 16


def _scrubber(case, salt=None):
    known = ([case["secret"]] if case.get("known") else []) + case.get("extra_secrets", [])
    hosts = [case["host"]] if case.get("host") else []
    return Scrubber(salt=salt, known_secrets=known, known_hosts=hosts)


@pytest.mark.parametrize("case", CORPUS["secrets"], ids=lambda c: c["name"])
def test_corpus_no_secret_survives(case):
    # "known" cases have no shape to match; the caller supplies the value.
    out = _scrubber(case).text(case["input"])
    assert case["secret"] not in out
    assert all(v not in out for v in case.get("extra_secrets", []))


@pytest.mark.parametrize("case", CORPUS["secrets"], ids=lambda c: c["name"])
def test_corpus_output_matches_expected(case):
    assert _scrubber(case, CORPUS_SALT).text(case["input"]) == case["expected"]


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


def test_corpus_has_expected_for_every_secret_case():
    assert all("expected" in c for c in CORPUS["secrets"])


@pytest.mark.parametrize("s", [
    "resolving \u200dmediabox.local failed", "resolving mediabox.local\ufe0f failed",
    "at fe80::1\u0301 ok", "\u00b2mediabox.local",
    "\u30b5\u30fc\u30d0\u30fcmediabox.local\u306b\u63a5\u7d9a",
])
def test_word_guards_are_ascii_only(s):
    assert "mediabox" not in Scrubber().text(s) and "fe80" not in Scrubber().text(s)


@pytest.mark.parametrize("s,gone", [
    ("password\u000b=hunter2 now", "hunter2"),
    ("Authorization\u000b: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Set-Cookie\u000b: sid=abc; Path=/", "sid=abc"),
    ("Authorization\x1c:\x85Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
])
def test_whitespace_class_is_explicit(s, gone):
    assert gone not in Scrubber().text(s)


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
    # Guards against quadratic blowups, which take tens of seconds on this
    # input, not micro speed. The bound is loose so a loaded box does not fail it.
    import time
    s = Scrubber()
    pem = "-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY-----"
    # A secret planted at the end must still be gone after the big input.
    for unit in (pem, "a.", "eyJ.", "-----BEGIN PRIVATE KEY-----", "a:", "token", "1.",
                 "[::", "x://", "x:///", "a://a@", "@", "a::", "to a:1 "):
        big = unit * (200_000 // len(unit)) + " password=PlantedSecret9 "
        t = time.perf_counter()
        out = s.text(big)
        assert time.perf_counter() - t < 3.0, unit
        assert "PlantedSecret9" not in out, unit


def test_surrogate_and_case_in_hashing():
    s = Scrubber()
    assert s.host("\ud800h")  # no UnicodeEncodeError
    assert s.address("http://\ud800h:1/").startswith("http://host-")
    assert s.text("2001:DB8::1") == s.text("2001:db8::1")
    assert "host-" in s.text("2001:DB8::1")


def test_known_host_with_port_single_word():
    s = Scrubber(known_hosts=["plexbox"])
    assert "plexbox" not in s.text("retry plexbox:32400 later")


def test_nested_url_masked_in_all_positions():
    s = Scrubber()
    out = s.text("a http://a.example.com/x?u=https://b.example.org/y#https://c.example.net/")
    for h in ("a.example.com", "b.example.org", "c.example.net"):
        assert h not in out


def test_every_secret_setting_any_module_knows_is_in_the_union():
    for k in (*NEVER_SENT, *dbstore.SECRET_SETTINGS, *backingstore.SECRET_KEYS):
        assert k in SECRET_SETTINGS_ALL


def test_every_secret_looking_setting_read_in_app_is_classified():
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    word = re.compile(r"password|token|secret|key|aud", re.I)
    seen = set()
    for f in root.rglob("*.py"):
        for m in re.finditer(r"get_setting\(\s*[\"']([a-z_0-9]+)[\"']", f.read_text()):
            seen.add(m.group(1))
    for k in seen:
        if word.search(k):
            assert k in SECRET_SETTINGS_ALL, f"{k} reads like a secret but is not listed"


# The fold of U+212A, U+017F, U+0130 and U+0131 must not act as an ASCII letter.
@pytest.mark.parametrize("ch", ["K", "ſ", "İ", "ı"])
def test_odd_case_folds_are_not_guards(ch):
    s = Scrubber(known_hosts=["couch.example.net"])
    assert "couch.example.net" not in s.text(ch + "couch.example.net")
    assert "mediabox.local" not in s.text(ch + "mediabox.local")
    assert "abcdefgh1" not in s.text("x" + ch + "Bearer abcdefgh1")


def test_host_hash_lowercases_ascii_only():
    s = Scrubber(salt=CORPUS_SALT)
    assert s.address("http://ABC.example/") == s.address("http://abc.example/")
    assert s.address("http://ΟΔ.example/") != s.address("http://οδ.example/")


def test_pem_hidden_behind_a_fake_begin_is_cut():
    s = "-----BEGIN A-----\nx\n-----END A-----BEGIN  -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    assert Scrubber().text(s) == "[removed]BEGIN  [removed]"


def test_cut_take():
    from app.reportscrub import cut_take
    assert cut_take("abc def", 20) == "abc def"
    assert cut_take("abc def ghi", 9) == "abc def "
    assert cut_take("abcdefghij", 5) == ""
    assert cut_take("a bcdef", 4) == "a "
