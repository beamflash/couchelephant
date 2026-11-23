"""The server's part and the Plex account email: who may ask, and that no
token ever comes back."""
import pytest

from app import auth, db, plextv
from tests import fake_plextv as fake


@pytest.fixture
def tvfake(monkeypatch):
    fake.STATE.reset()
    url, stop = fake.start()
    monkeypatch.setattr(plextv, "BASE", url)
    yield fake.STATE
    stop()


def _signin_on():
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    kid = auth.create_user("kid", "a-good-password", role="user")
    return admin, kid


def _device_headers(client):
    asked = client.post("/api/tv/pair", data={"name": "Den"}).json()
    key = client.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def test_server_part_open_when_signin_off(client):
    r = client.get("/api/report/server")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["server"]["version"]


def test_server_part_admin_session_ok(client):
    admin, _ = _signin_on()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    assert client.get("/api/report/server").status_code == 200


def test_server_part_non_admin_403(client):
    _, kid = _signin_on()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(kid))
    r = client.get("/api/report/server")
    assert r.status_code == 403
    assert r.json()["error"] == "administrators only"


def test_server_part_paired_device_key_ok(client):
    hdr = _device_headers(client)
    _signin_on()
    assert client.get("/api/report/server", headers=hdr).status_code == 200
    assert client.get("/api/report/email", headers=hdr).status_code == 200


def test_server_part_no_key_401(client):
    _signin_on()
    assert client.get("/api/report/server").status_code == 401
    assert client.get("/api/report/email").status_code == 401


def test_server_part_body_has_no_token(client):
    db.set_setting("plex_token", fake.TOKEN)
    r = client.get("/api/report/server")
    assert r.status_code == 200
    assert fake.TOKEN not in r.text


def test_server_part_timeout_still_answers(client, monkeypatch):
    from app.routes import report
    monkeypatch.setattr(report, "DEADLINE", 0.05)
    monkeypatch.setattr(report.reportbuild, "server_part",
                        lambda s: __import__("time").sleep(0.5))
    j = client.get("/api/report/server").json()
    assert j == {"ok": True, "server": None, "note": "server part timed out"}


def test_email_returns_plex_account_email(client, tvfake):
    db.set_setting("plex_token", fake.TOKEN)
    tvfake.email = "me@example.com"
    assert client.get("/api/report/email").json() == {"ok": True, "email": "me@example.com"}


def test_email_empty_without_plex_signin(client, tvfake):
    db.set_setting("plex_token", "")
    j = client.get("/api/report/email").json()
    assert j == {"ok": True, "email": ""}
    assert tvfake.requests == []


def test_email_empty_when_plextv_down(client, tvfake):
    db.set_setting("plex_token", fake.TOKEN)
    tvfake.fail_status = 500
    r = client.get("/api/report/email")
    assert r.status_code == 200 and r.json() == {"ok": True, "email": ""}


def test_email_response_never_contains_token(client, tvfake):
    db.set_setting("plex_token", fake.TOKEN)
    tvfake.email = "me@example.com"
    assert fake.TOKEN not in client.get("/api/report/email").text
    tvfake.fail_status = 401
    assert fake.TOKEN not in client.get("/api/report/email").text


def test_server_part_failure_answers_note(client, monkeypatch):
    from app.routes import report

    def boom(s):
        raise ValueError("secret detail")
    monkeypatch.setattr(report.reportbuild, "server_part", boom)
    r = client.get("/api/report/server")
    assert r.json() == {"ok": True, "server": None, "note": "server part failed"}
    assert "secret detail" not in r.text


def test_email_timeout_is_empty(client, tvfake, monkeypatch):
    from app.routes import report
    db.set_setting("plex_token", fake.TOKEN)
    monkeypatch.setattr(report, "DEADLINE", 0.05)
    monkeypatch.setattr(report.plextv, "account_email",
                        lambda t: __import__("time").sleep(0.5))
    assert client.get("/api/report/email").json() == {"ok": True, "email": ""}


def test_send_and_consent_are_not_admin_only():
    from app.web import _admin_only
    assert _admin_only("/api/report/server", "GET")
    assert _admin_only("/api/report/email", "GET")
    assert not _admin_only("/api/report/send", "POST")
    assert not _admin_only("/api/report/consent", "POST")


# ---- consent and send ----

import base64
import gzip
import json
from pathlib import Path

from app import reportsend, sealing
from tests import fake_firestore
from tests.fake_firestore import STATE

VEC = json.loads((Path(__file__).parent / "data" / "seal_vector.json").read_text())
PRIV = base64.b64decode(VEC["private"])


@pytest.fixture
def fs(client, monkeypatch):
    url, stop = fake_firestore.start()
    STATE.reset()
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PUBLIC_KEY", VEC["public"])
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PROJECT", fake_firestore.PROJECT)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_API_KEY", fake_firestore.API_KEY)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_FIRESTORE_URL", url)
    yield STATE
    stop()


def _body(**kw):
    b = {"trigger": {"kind": "support"}, "message": "hello zebra", "email": "",
         "client": {"ua": "test", "screen_size": "10x10"}, "app_log": ["a line"]}
    b.update(kw)
    return b


def _agree(client):
    assert client.post("/api/report/consent", data={"agree": "1"}).json() == {"ok": True}


def _opened(fs):
    doc = next(iter(fs.docs.values()))
    sealed = base64.b64decode(doc["sealed"]["bytesValue"])
    return json.loads(gzip.decompress(sealing.open_sealed(sealed, PRIV)))


def test_send_refused_before_consent(client, fs):
    assert client.get("/api/report/consent").json() == {"ok": True, "consented": False,
                                                        "queued": 0}
    r = client.post("/api/report/send", json=_body())
    assert r.status_code == 403 and r.json() == {"ok": False, "error": "consent"}
    assert not fs.docs


def test_consent_then_send_creates_one_sealed_doc(client, fs):
    _agree(client)
    assert client.get("/api/report/consent").json()["consented"] is True
    j = client.post("/api/report/send", json=_body()).json()
    assert j["ok"] and j["sent"] and not j["queued"]
    assert j["message"] == "Sent. Thank you." and len(j["reportId"]) == 32
    assert len(fs.docs) == 1
    rep = _opened(fs)
    assert rep["message"] == "hello zebra" and rep["source"] == "web"
    assert rep["reportId"] == j["reportId"] and rep["appLog"] == ["a line"]


def test_consent_needs_agree(client):
    assert client.post("/api/report/consent", data={"agree": "0"}).status_code == 400
    assert client.get("/api/report/consent").json()["consented"] is False


def test_send_includes_server_part(client, fs):
    _agree(client)
    client.post("/api/report/send", json=_body())
    assert _opened(fs)["server"]["version"]


def test_send_with_slow_server_part_still_sends(client, fs, monkeypatch):
    from app.routes import report
    monkeypatch.setattr(report, "DEADLINE", 0.05)
    monkeypatch.setattr(report.reportbuild, "server_part",
                        lambda s: __import__("time").sleep(0.5))
    _agree(client)
    assert client.post("/api/report/send", json=_body()).json()["sent"]
    rep = _opened(fs)
    assert rep["server"] is None and rep["serverNote"] == "server part timed out"


def test_send_failure_queues_and_says_saved_copy(client, fs):
    _agree(client)
    fs.fail_next(503)
    j = client.post("/api/report/send", json=_body()).json()
    assert j["ok"] and j["queued"] and not j["sent"]
    assert j["message"] == "Could not send. It is saved and will be sent later."
    assert client.get("/api/report/consent").json()["queued"] == 1


def test_non_admin_signed_in_person_may_send(client, fs):
    _, kid = _signin_on()
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(kid))
    _agree(client)
    assert client.post("/api/report/send", json=_body()).json()["sent"]
    assert client.get("/api/report/server").status_code == 403


def test_signed_out_cannot_send_when_signin_on(client, fs):
    _signin_on()
    assert client.post("/api/report/send", json=_body()).status_code == 401
    assert client.post("/api/report/consent", data={"agree": "1"}).status_code == 401


def test_email_validation_cases(client, fs):
    _agree(client)
    for ok in ("", "a@b.co"):
        assert client.post("/api/report/send", json=_body(email=ok)).status_code == 200
    for bad in ("junk", "a@" + "b" * 300, "a b@c.co"):
        r = client.post("/api/report/send", json=_body(email=bad))
        assert r.status_code == 400
        assert r.json()["error"] == "That email address does not look right."


def test_message_over_cap_refused(client, fs):
    _agree(client)
    assert client.post("/api/report/send", json=_body(message="x" * 20_000)).status_code == 200
    assert client.post("/api/report/send", json=_body(message="x" * 20_001)).status_code == 400


def test_crash_kind_accepted_and_bad_kind_refused(client, fs):
    _agree(client)
    assert client.post("/api/report/send",
                       json=_body(trigger={"kind": "crash"})).status_code == 200
    assert client.post("/api/report/send",
                       json=_body(trigger={"kind": "nope"})).status_code == 400


def test_double_post_same_second_does_not_duplicate(client, fs):
    _agree(client)
    a = client.post("/api/report/send", json=_body()).json()
    b = client.post("/api/report/send", json=_body()).json()
    assert a["reportId"] != b["reportId"] and len(fs.docs) == 2
    # One report id through the queue twice is still one document.
    fs.reset()
    fs.fail_next(503)
    c = client.post("/api/report/send", json=_body()).json()
    assert c["queued"]
    assert reportsend.flush_queue() == 1
    assert len(fs.docs) == 1 and c["reportId"] in fs.docs


def test_rate_limit_message_is_plain(client, fs, monkeypatch):
    monkeypatch.setattr(reportsend, "LIMIT_PER_DAY", 1)
    _agree(client)
    assert client.post("/api/report/send", json=_body()).json()["sent"]
    j = client.post("/api/report/send", json=_body()).json()
    assert j["ok"] is False and "too many reports today" in j["error"].lower()
    assert "tomorrow" in j["error"].lower()


def test_docs_name_every_report_route():
    from pathlib import Path

    from app.routes import report
    api = (Path(__file__).resolve().parent.parent / "docs" / "API.md").read_text()
    paths = {(m, r.path) for r in report.router.routes for m in r.methods}
    assert paths
    for method, path in paths:
        assert f"`{method} {path}`" in api, f"docs/API.md does not name {method} {path}"


def test_secret_straddling_the_line_cap_does_not_survive(client, fs):
    # A 16 hex device-style value has no shape of its own, so only a known
    # secret rule can catch it, and only if the cap comes after.
    token = "0a1b2c3d4e5f6071"
    db.set_setting("plex_token", token)
    _agree(client)
    line = "x " * 995 + token + " tail"
    body = _body(app_log=[line], trigger={"kind": "error", "error": line, "screen": line})
    assert client.post("/api/report/send", json=body).json()["ok"]
    rep = _opened(fs)
    for text in (rep["appLog"][0], rep["trigger"]["error"], rep["trigger"]["screen"]):
        assert token[:6] not in text and len(text) <= 2000


def test_line_cut_at_the_take_limit_loses_its_cut_word(client, fs):
    # Scrubbing shrinks the long prefix, which would pull a half-cut secret
    # into the kept 2000 if the cut word stayed.
    key = "Zk3q9XvB2mN7pL4rT8wY1cD6fH0jS5aE"
    db.set_setting("plex_token", key)
    _agree(client)
    shrinkable = "token=" + "Q" * 7978 + " " + key
    nowhite = "a" * 9000
    body = _body(app_log=[shrinkable, nowhite],
                 trigger={"kind": "error", "error": shrinkable, "screen": nowhite})
    assert client.post("/api/report/send", json=body).json()["ok"]
    rep = _opened(fs)
    for text in (rep["appLog"][0], rep["trigger"]["error"]):
        assert key[:6] not in text
    assert rep["appLog"][1] == "" and rep["trigger"]["screen"] == ""
