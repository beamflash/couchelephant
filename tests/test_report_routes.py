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
