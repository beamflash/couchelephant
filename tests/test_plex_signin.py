"""Sign in with Plex: the routes, against a fake plex.tv and the fake Plex."""
import logging

import pytest

from app import auth, db, plextv
from app.routes import settings as routes
from tests import fake_plextv as fake

H = {"Accept": "application/json"}


@pytest.fixture
def tv(monkeypatch):
    fake.STATE.reset()
    url, stop = fake.start()
    monkeypatch.setattr(plextv, "BASE", url)
    routes._held.clear()
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    yield fake.STATE
    stop()


def _start(client):
    r = client.post("/settings/plex/signin", headers=H)
    assert r.status_code == 200
    return r.json()


def _approved(client, tv, servers):
    tv.load_resources(servers)
    pin = _start(client)["pin"]
    tv.approve(pin)
    client.get(f"/settings/plex/signin/{pin}")     # the page's poll takes the token
    return pin


def test_start_gives_the_code_and_link(client, tv):
    j = _start(client)
    assert j["ok"] is True and j["code"] == "ABCD"
    assert isinstance(j["pin"], int)
    assert j["link"] == "https://plex.tv/link"


def test_polling_waits_until_approved(client, tv):
    pin = _start(client)["pin"]
    assert client.get(f"/settings/plex/signin/{pin}").json() == {"ok": True, "state": "waiting"}


def test_an_approved_pin_lists_the_servers_without_any_token(client, tv, plex_url):
    pin = _approved(client, tv, [
        fake.server("Den", "cid-1", [(plex_url, True, False)], token="srv-secret-1"),
        fake.server("Attic", "cid-2", [(plex_url, True, False)], token="srv-secret-2")])
    r = client.get(f"/settings/plex/signin/{pin}")
    assert r.json() == {"ok": True, "state": "servers", "servers": [
        {"name": "Den", "id": "cid-1"}, {"name": "Attic", "id": "cid-2"}]}
    for secret in (fake.TOKEN, "srv-secret-1", "srv-secret-2"):
        assert secret not in r.text


def test_use_saves_the_server_token_and_starts_a_sync(client, tv, plex_url):
    pin = _approved(client, tv, [fake.server(
        "Den", "cid-1", [(plex_url, True, False)], token="srv-secret-1")])
    db.set_setting("dry_run", "0")
    r = client.post(f"/settings/plex/signin/{pin}/use", headers=H,
                    data={"server": "cid-1", "timezone": "America/Chicago"})
    j = r.json()
    assert j["ok"] is True and j["address"] == plex_url
    assert "srv-secret-1" not in r.text and fake.TOKEN not in r.text
    assert db.get_setting("plex_url") == plex_url
    assert db.get_setting("plex_token") == "srv-secret-1"
    assert db.get_setting("dry_run") == "1"
    assert db.get_setting("timezone") == "America/Chicago"


def test_use_leaves_the_timezone_alone_when_none_is_sent(client, tv, plex_url):
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"})
    assert db.get_setting("timezone") == "UTC"


def test_use_skips_a_dead_address_and_saves_the_next(client, tv, plex_url):
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [
        ("http://127.0.0.1:1", True, False), (plex_url, False, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["ok"] is True and db.get_setting("plex_url") == plex_url


def test_a_server_with_no_dvr_says_so_and_saves_nothing(client, tv, plex_url, monkeypatch):
    from app.plex import Plex
    monkeypatch.setattr(Plex, "dvrs", lambda self: [])
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["ok"] is False and "has no DVR" in j["detail"]
    assert j["tried"] == [plex_url]
    assert db.get_setting("plex_url") == ""


def test_when_every_address_fails_each_is_listed(client, tv):
    a, b = "http://127.0.0.1:1", "http://127.0.0.1:2"
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(a, True, False), (b, False, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["ok"] is False and j["tried"] == [a, b]
    assert "Could not reach" in j["detail"]
    assert db.get_setting("plex_token") == ""


def test_use_with_an_unknown_pin_or_server_fails_plainly(client, tv, plex_url):
    j = client.post("/settings/plex/signin/999/use", data={"server": "x"}).json()
    assert j["ok"] is False
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "nope"}).json()
    assert j["ok"] is False and db.get_setting("plex_url") == ""


def test_an_expired_pin_says_expired(client, tv):
    pin = _start(client)["pin"]
    tv.expire(pin)
    assert client.get(f"/settings/plex/signin/{pin}").json() == {"ok": False, "state": "expired"}
    assert pin not in routes._held


def test_a_pin_past_its_time_says_expired_and_drops_the_token(client, tv):
    pin = _start(client)["pin"]
    routes._held[pin]["expires"] = 1
    assert client.get(f"/settings/plex/signin/{pin}").json()["state"] == "expired"
    assert pin not in routes._held


def test_held_tokens_are_pruned_after_twenty_minutes(client, tv):
    pin = _approved(client, tv, [])
    assert routes._held[pin]["token"] == fake.TOKEN
    routes._held[pin]["at"] -= routes.HELD_SECONDS + 1
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "x"}).json()
    assert j["ok"] is False and pin not in routes._held


def test_a_rejected_sign_in_is_reported_without_the_token(client, tv, caplog):
    pin = _start(client)["pin"]
    tv.approve(pin, token="tok-not-valid")
    tv.valid_tokens.discard("tok-not-valid")
    with caplog.at_level(logging.DEBUG):
        r = client.get(f"/settings/plex/signin/{pin}")
    assert r.json()["ok"] is False and "tok-not-valid" not in r.text
    assert "tok-not-valid" not in caplog.text


def test_open_on_first_run(client, tv):
    assert client.post("/settings/plex/signin", headers=H).json()["ok"] is True


def test_administrators_only_with_sign_in_on(client, tv):
    db.set_setting("auth_mode", "local")
    auth.create_user("boss", "a-long-enough-password", role="admin")
    auth.create_user("kid", "a-long-enough-password", role="user")
    r = client.post("/settings/plex/signin", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    kid = auth.create_session(db_user("kid"))
    client.cookies.set(auth.SESSION_COOKIE, kid)
    assert client.post("/settings/plex/signin").status_code == 403
    assert client.get("/settings/plex/signin/1").status_code == 403
    assert client.post("/settings/plex/signin/1/use", data={"server": "x"}).status_code == 403
    boss = auth.create_session(db_user("boss"))
    client.cookies.set(auth.SESSION_COOKIE, boss)
    assert client.post("/settings/plex/signin").status_code == 200


def db_user(name):
    return next(u["id"] for u in auth.list_users() if u["username"] == name)


# ---- fix round 1 ----

@pytest.fixture
def silent_ports():
    """Addresses that accept a connection and never answer."""
    import socket
    socks = []

    def make():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        socks.append(s)
        return f"http://127.0.0.1:{s.getsockname()[1]}"
    yield make
    for s in socks:
        s.close()


def test_addresses_that_hang_do_not_hold_use_past_the_cap(client, tv, silent_ports):
    import time
    urls = [silent_ports() for _ in range(4)]
    pin = _approved(client, tv, [fake.server(
        "Den", "cid-1", [(u, i == 0, False) for i, u in enumerate(urls)])])
    t0 = time.monotonic()
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert time.monotonic() - t0 < 11
    assert j["ok"] is False and j["tried"] == urls
    assert db.get_setting("plex_token") == ""


def test_a_hanging_local_address_gives_way_to_a_working_remote(client, tv, silent_ports, plex_url):
    dead = silent_ports()
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [
        (dead, True, False), (plex_url, False, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["ok"] is True and j["address"] == plex_url


def test_the_preferred_address_wins_even_if_another_answers_first(client, tv, plex_url, monkeypatch):
    import time
    seen = {}
    real = routes._test_plex
    other = plex_url.replace("127.0.0.1", "localhost")

    def slow_first(url, token, timeout=None):
        if url == plex_url:
            time.sleep(0.5)
        seen[url] = True
        return real(url, token, timeout)
    monkeypatch.setattr(routes, "_test_plex", slow_first)
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [
        (plex_url, True, False), (other, False, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["address"] == plex_url


def test_a_first_sign_in_starts_in_preview(client, tv, plex_url):
    db.set_setting("dry_run", "0")
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"})
    assert db.get_setting("dry_run") == "1"


def test_signing_in_again_keeps_the_current_preview_choice(client, tv, plex_url):
    db.set_setting("plex_url", "http://old:32400")
    db.set_setting("dry_run", "0")
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    j = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert j["ok"] is True and db.get_setting("plex_url") == plex_url
    assert db.get_setting("dry_run") == "0"


def test_a_late_poll_after_use_does_not_take_the_token_again(client, tv, plex_url):
    pin = _approved(client, tv, [fake.server("Den", "cid-1", [(plex_url, True, False)])])
    assert client.post(f"/settings/plex/signin/{pin}/use",
                       data={"server": "cid-1"}).json()["ok"] is True
    j = client.get(f"/settings/plex/signin/{pin}").json()
    assert j == {"ok": False, "state": "expired"}
    assert routes._held[pin]["token"] is None
    again = client.post(f"/settings/plex/signin/{pin}/use", data={"server": "cid-1"}).json()
    assert again["ok"] is False
