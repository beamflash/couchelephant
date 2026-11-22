import base64
import gzip
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import pytest

from app import db, logring, reportbuild, reportscrub, sealing
from app.reportbuild import ReportTooLarge, build_report, make_scrubber, pack, server_part
from app.reportscrub import Scrubber

log = logging.getLogger("couchelephant")
VEC = json.load(open(os.path.join(os.path.dirname(__file__), "data", "seal_vector.json")))
PRIV = base64.b64decode(VEC["private"])
PUB = base64.b64decode(VEC["public"])
TOKEN = "PLEXSECRET123456789012"


@pytest.fixture(autouse=True)
def ring():
    logring.install()
    logring.clear()
    yield
    logring.clear()


def _report(**kw):
    args = dict(source="web", app={"v": "1"}, trigger={"kind": "manual"},
                app_log=[], server=None, server_note=None,
                message="hi", email="", scrub=Scrubber(salt=b"s"))
    args.update(kw)
    return build_report(**args)


def test_server_part_has_expected_keys(client):
    p = server_part(make_scrubber())
    assert set(p) == {"version", "uptime_seconds", "os", "container", "python",
                      "log", "settings", "tuner", "recorder", "passes",
                      "schedule_failures", "notices", "disk"}
    assert isinstance(p["container"], bool)
    assert set(p["recorder"]) == {"available", "why_unavailable", "encoder"}
    assert set(p["tuner"]) == {"url", "detected"}


def test_server_part_never_contains_known_secrets(client):
    db.set_setting("plex_token", TOKEN)
    db.set_setting("tmdb_key", "TMDBSECRET98765")
    db.set_setting("sportsdb_key", "SPORTSSECRET4321")
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    log.info("plex answered for %s with %s and %s", TOKEN, "TMDBSECRET98765",
             "SPORTSSECRET4321")
    log.info("pair code %s", asked["code"])
    out = json.dumps(server_part(make_scrubber()))
    for s in (TOKEN, "TMDBSECRET98765", "SPORTSSECRET4321"):
        assert s not in out
    # The device's poll secret is only ever stored hashed.
    row = db.one("SELECT poll_hash FROM tv_devices")
    assert asked["secret"] not in out and row["poll_hash"] not in out
    assert urlsplit(db.get_setting("plex_url")).hostname not in out
    assert "plex answered" in out
    assert asked["code"] not in out


def test_bare_no_digit_token_removed_via_make_scrubber(client):
    tok = "ABCDEFGHIJKLMNOPQRST"
    db.set_setting("plex_token", tok)
    log.info("sent %s upstream", tok)
    # The deny patterns alone do not know this one.
    assert tok in Scrubber().text(f"sent {tok} upstream")
    out = json.dumps(server_part(make_scrubber()))
    assert tok not in out
    assert "[removed]" in out


def test_settings_come_only_from_allow_list(client):
    db.set_setting("some_new_secret_thing", "zzz")
    s = server_part(make_scrubber())["settings"]
    allowed = set(reportscrub.SETTINGS_ALLOW) | set(reportscrub.ADDRESS_SETTINGS)
    assert set(s) <= allowed
    assert "plex_token" not in s and "some_new_secret_thing" not in s


def test_disk_space_reported(client):
    d = server_part(make_scrubber())["disk"]
    assert d["total_bytes"] > 0 and 0 <= d["free_bytes"] <= d["total_bytes"]


def test_passes_and_failures_listed(client):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_name, enabled) VALUES ('team','Red Sox',1)")
        c.execute("INSERT INTO recordings (uid, state, title, begins_at, error) "
                  "VALUES ('u1','failed','Game',100,'no tuner at 192.168.1.5')")
    p = server_part(make_scrubber())
    assert p["passes"] == [{"kind": "team", "title": "Red Sox", "enabled": True}]
    f = p["schedule_failures"][0]
    assert f["title"] == "Game" and "192.168.1.5" not in f["error"]


def test_build_report_message_and_email_verbatim():
    r = _report(message="my password=hunter2 broke", email="a@b.example")
    assert r["message"] == "my password=hunter2 broke"
    assert r["email"] == "a@b.example"
    assert r["v"] == 1 and r["source"] == "web"


def test_build_report_scrubs_app_log():
    r = _report(app_log=["got token=abc123 ok"], trigger={"note": "token=zzz"})
    assert "abc123" not in json.dumps(r["appLog"])
    assert "[removed]" in r["appLog"][0]
    assert "zzz" not in json.dumps(r["trigger"])


def test_report_id_is_32_hex_and_random():
    a, b = _report(), _report()
    assert re.fullmatch(r"[0-9a-f]{32}", a["reportId"]) and a["reportId"] != b["reportId"]
    assert _report(report_id="ab" * 16)["reportId"] == "ab" * 16
    with pytest.raises(ValueError):
        _report(report_id="x" * 32)


def test_bad_source_refused():
    with pytest.raises(ValueError):
        _report(source="android")


def _open(blob):
    return json.loads(gzip.decompress(sealing.open_sealed(blob, PRIV)))


def test_pack_round_trips_with_private_key():
    r = _report(app_log=["one", "two"])
    assert _open(pack(r, public=PUB)) == r


def test_pack_truncates_old_log_when_over_cap():
    lines = [f"{i:07d} " + " ".join(os.urandom(4).hex() for _ in range(10))
             for i in range(15000)]
    r = _report(app_log=lines, server={"log": list(lines)},
                trigger={"kind": "crash"}, message="keep me")
    blob = pack(r, public=PUB)
    assert len(blob) < 900_000
    got = _open(blob)
    assert got["trigger"] == {"kind": "crash"} and got["message"] == "keep me"
    assert got["appLog"][-1] == lines[-1] and got["server"]["log"][-1] == lines[-1]
    assert len(got["appLog"]) < len(lines)


def test_pack_never_drops_message():
    r = _report(message=os.urandom(5000).hex())
    with pytest.raises(ReportTooLarge):
        pack(r, cap=2000, public=PUB)


def test_pack_needs_a_key():
    with pytest.raises(ValueError):
        pack(_report())


def test_backing_store_password_in_any_shape_is_removed(client):
    db.set_setting("pg_password", "hunter")
    db.set_setting("my_password", "correct horse")
    log.info("connect failed for hunter, also correct horse")
    out = json.dumps(server_part(make_scrubber()))
    assert "hunter" not in out and "correct horse" not in out
    r = _report(app_log=["bad login hunter"], scrub=make_scrubber())
    assert "hunter" not in json.dumps(r)


def test_bare_backing_store_host_is_masked(client):
    db.set_setting("pg_host", "nas")
    db.set_setting("away_domain", "tv.example.org")
    log.info("could not reach nas for tv.example.org")
    out = json.dumps(server_part(make_scrubber()))
    assert "reach nas " not in out
    assert "could not reach host-" in out
    assert "tv.example.org" not in out


def test_server_part_does_not_wait_on_the_encoder_probe(client, monkeypatch):
    from app import recorder
    monkeypatch.setattr(recorder, "_vaapi", None)
    monkeypatch.setattr(recorder, "_vaapi_works", lambda: time.sleep(20))
    db.set_setting("recorder_encoder", "auto")
    t = time.time()
    p = server_part(make_scrubber())
    assert time.time() - t < 5
    assert p["recorder"]["encoder"] == "unknown (not probed yet)"


def test_encoder_reads_the_cached_probe(client, monkeypatch):
    from app import recorder
    db.set_setting("recorder_encoder", "auto")
    monkeypatch.setattr(recorder, "_vaapi", True)
    assert server_part(make_scrubber())["recorder"]["encoder"] == "vaapi"
    monkeypatch.setattr(recorder, "_vaapi", False)
    assert server_part(make_scrubber())["recorder"]["encoder"] == "software"


def test_scrubbed_keys_in_a_report():
    r = _report(trigger={"token=abc123": 1})
    assert "abc123" not in json.dumps(r["trigger"])


def test_too_large_with_logs_gone_says_so():
    r = _report(app_log=["a"], message="x", trigger={"k": " ".join(os.urandom(4).hex() for _ in range(900))})
    with pytest.raises(ReportTooLarge, match="even without logs"):
        pack(r, cap=2000, public=PUB)


def test_uptime_is_since_process_start(client):
    assert server_part(make_scrubber())["uptime_seconds"] >= 0
    assert reportbuild._STARTED <= time.time()
