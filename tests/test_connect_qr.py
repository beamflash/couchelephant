"""The QR code that connects a phone."""
import urllib.parse

import pytest
import segno

from app import announce, auth, db, paths
from app.routes import tv

THIS = "http://192.168.1.20:8710"


@pytest.fixture(autouse=True)
def _outside_a_container(monkeypatch):
    # CI runs the suite inside a container. Tests that want the container
    # case set it themselves.
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    # "In a container" in these tests means on a Docker bridge.
    monkeypatch.setattr(announce, "_host_network", lambda: False)

@pytest.fixture(autouse=True)
def _not_in_a_container(monkeypatch):
    """CI runs inside a container; tests that care set this themselves."""
    from app import paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)



def _expected(url):
    link = "couchelephant://connect?url=" + urllib.parse.quote(url, safe="")
    return segno.make(link).svg_inline(**tv.QR_LOOK)


def _body(r):
    return r.text.replace(' xmlns="http://www.w3.org/2000/svg"', "", 1)


def _machine(monkeypatch, *ips):
    import socket
    monkeypatch.setattr(announce, "_addresses",
                        lambda: [socket.inet_aton(i) for i in ips])


def test_a_machine_address_gets_the_servers_own_port(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8123")
    r = client.get("/api/connect.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert 'xmlns="http://www.w3.org/2000/svg"' in r.text
    assert paths.port() == 8123
    assert _body(r) == _expected("http://192.168.1.20:8123")


def test_a_lan_host_keeps_the_port_the_browser_used(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.99")
    r = client.get("/api/connect.svg", headers={"Host": "192.168.1.20:8710"})
    assert _body(r) == _expected(THIS)
    r = client.get("/api/connect.svg", headers={"Host": "den.local:8710"})
    assert _body(r) == _expected("http://den.local:8710")


def test_a_lan_host_with_no_port_has_none_in_the_url(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.99")
    r = client.get("/api/connect.svg", headers={"Host": "192.168.1.20"})
    assert _body(r) == _expected("http://192.168.1.20")


def test_the_internal_listen_port_is_never_used(client, monkeypatch):
    # Behind a proxy the app listens on another port than the browser used.
    _machine(monkeypatch, "192.168.1.20")
    from app.routes import tv

    class R:
        url = type("U", (), {"hostname": "192.168.1.20", "port": None,
                             "scheme": "http"})()
        scope = {"server": ("0.0.0.0", 9999)}
    assert tv.lan_url(R()) == "http://192.168.1.20"


def test_a_public_or_loopback_host_is_swapped_for_the_machine(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    for host in ("127.0.0.1:8710", "localhost:8710", "example.com:8710"):
        r = client.get("/api/connect.svg", headers={"Host": host})
        assert _body(r) == _expected(THIS), host


def test_an_ipv6_host_is_swapped_for_the_machines_ipv4(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    r = client.get("/api/connect.svg", headers={"Host": "[fe80::1]:8710"})
    assert _body(r) == _expected(THIS)


def test_a_local_name_is_trusted_on_purpose(client, monkeypatch):
    # The browser sends the Host it went to, so a .local name in it is a name
    # the person's own network answered. evil.local is accepted by design.
    _machine(monkeypatch, "192.168.1.20")
    r = client.get("/api/connect.svg", headers={"Host": "evil.local:8710"})
    assert _body(r) == _expected("http://evil.local:8710")


def test_the_docker_bridge_is_left_out(client, monkeypatch):
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    _machine(monkeypatch, "192.168.1.20")
    r = client.get("/api/connect.svg", headers={"Host": "172.17.0.2:8710"})
    assert _body(r) == _expected(THIS)


def test_a_home_address_beats_a_vpn_one_listed_first(client, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    for first in ("100.64.3.4", "172.18.0.5"):
        _machine(monkeypatch, first, "10.0.0.7", "192.168.1.20")
        assert _body(client.get("/api/connect.svg")) == _expected(THIS), first
    _machine(monkeypatch, "100.64.3.4", "172.18.0.5", "10.0.0.7")
    assert _body(client.get("/api/connect.svg")) == _expected("http://10.0.0.7:8710")
    _machine(monkeypatch, "100.64.3.4", "172.18.0.5")
    assert _body(client.get("/api/connect.svg")) == _expected("http://172.18.0.5:8710")
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert _body(client.get("/api/connect.svg")) == _expected("http://100.64.3.4:8710")
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    _machine(monkeypatch, "100.64.3.4")
    assert _body(client.get("/api/connect.svg")) == _expected("http://100.64.3.4:8710")


def test_with_no_usable_address_it_says_so_instead_of_guessing(client, monkeypatch):
    _machine(monkeypatch)
    r = client.get("/api/connect.svg", headers={"Host": "localhost:8710"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "detail": "This server has no home network "
                        "address the apps can use. Type its address in the app instead."}


def test_the_payload_is_exactly_the_link_and_nothing_else(client, monkeypatch):
    """The route's real output must equal the code for the bare link. Any added
    fp, key or token changes the code, so this fails."""
    _machine(monkeypatch, "192.168.1.20")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    bare = "couchelephant://connect?url=http%3A%2F%2F192.168.1.20%3A8710"
    got = _body(client.get("/api/connect.svg"))
    assert got == segno.make(bare).svg_inline(**tv.QR_LOOK)
    for extra in ("&fp=abc", "&key=abc", "&token=abc"):
        assert got != segno.make(bare + extra).svg_inline(**tv.QR_LOOK)


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


def test_an_https_proxy_name_gives_the_machines_plain_http_address(client, monkeypatch):
    # Reached through a proxy at https://tv.example.org, the phone should use
    # the server's own port, which speaks plain http.
    _machine(monkeypatch, "192.168.1.20")
    r = client.get("https://tv.example.org/api/connect.svg")
    assert _body(r) == _expected("http://192.168.1.20:8710")


def test_in_a_container_a_172_18_host_is_a_bridge_too(client, monkeypatch):
    _machine(monkeypatch, "192.168.1.20")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    host = {"Host": "172.18.0.5:8710"}
    assert _body(client.get("/api/connect.svg", headers=host)) == _expected("http://172.18.0.5:8710")
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert _body(client.get("/api/connect.svg", headers=host)) == _expected(THIS)


def test_the_address_setting_wins_over_the_machine(client, monkeypatch):
    # A container on Docker's bridge has no home address of its own.
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    _machine(monkeypatch, "172.17.0.2")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    assert client.get("/api/connect.svg").status_code == 409
    monkeypatch.setenv("COUCHELEPHANT_ADDRESS", "192.168.1.20")
    assert _body(client.get("/api/connect.svg")) == _expected(THIS)


def test_a_host_network_container_on_a_172_lan_keeps_its_address(client, monkeypatch):
    # Host networking on a home LAN in 172.16-31: that address is the real one.
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(announce, "_host_network", lambda: True)
    _machine(monkeypatch, "172.25.9.7")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8710")
    assert _body(client.get("/api/connect.svg")) == _expected("http://172.25.9.7:8710")
    assert not announce.is_bridge("172.25.9.7")
