"""The QR code that connects a phone."""
import urllib.parse

import segno

from app import announce, auth, db
from app.routes import tv

THIS = "http://192.168.1.20:8710"


def _expected(url):
    link = "couchelephant://connect?url=" + urllib.parse.quote(url, safe="")
    return segno.make(link).svg_inline(**tv.QR_LOOK)


def _body(r):
    return r.text.replace(' xmlns="http://www.w3.org/2000/svg"', "", 1)


def _machine(monkeypatch, *ips):
    import socket
    monkeypatch.setattr(announce, "_addresses",
                        lambda: [socket.inet_aton(i) for i in ips])


def test_the_code_carries_this_servers_lan_address(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    r = client.get("/api/connect.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert 'xmlns="http://www.w3.org/2000/svg"' in r.text
    assert _body(r) == _expected("http://192.168.1.20:80")


def test_a_lan_host_in_the_request_is_used_as_it_is(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.99")
    r = client.get("/api/connect.svg", headers={"Host": "192.168.1.20:8710"})
    assert _body(r) == _expected(THIS)
    r = client.get("/api/connect.svg", headers={"Host": "den.local:8710"})
    assert _body(r) == _expected("http://den.local:8710")


def test_a_public_or_loopback_host_is_swapped_for_the_machine(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    for host in ("127.0.0.1:8710", "localhost:8710", "example.com:8710"):
        r = client.get("/api/connect.svg", headers={"Host": host})
        assert _body(r) == _expected(THIS), host


def test_the_docker_bridge_is_left_out(client, monkeypatch):
    # announce._addresses already drops 172.17.x in a container; a request
    # that names it is not a LAN host either.
    from app import paths
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    _machine(monkeypatch, "192.168.1.20")
    r = client.get("/api/connect.svg", headers={"Host": "172.17.0.2:8710"})
    assert _body(r) == _expected(THIS)


def test_the_payload_has_no_key_or_token(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    link = "couchelephant://connect?url=" + urllib.parse.quote(THIS, safe="")
    assert "key" not in link and "token" not in link
    assert link == "couchelephant://connect?url=http%3A%2F%2F192.168.1.20%3A8710"


def test_with_sign_in_on_only_an_administrator_gets_it(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    kid = auth.create_user("kid", "a-good-password", role="user")
    assert client.get("/api/connect.svg").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(kid))
    assert client.get("/api/connect.svg").status_code == 403
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    assert client.get("/api/connect.svg").status_code == 200
