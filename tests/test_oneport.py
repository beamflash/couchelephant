"""One port, HTTP and HTTPS, on a real server."""
import json
import socket
import ssl
import threading
import time

import httpx
import pytest
import uvicorn
from cryptography import x509

from app import announce, db, exposure, oneport, paths, tls


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path))
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    monkeypatch.setattr(announce, "_addresses",
                        lambda: [socket.inet_aton("192.168.1.20")])
    monkeypatch.setattr(exposure, "_adapters", lambda: [])
    monkeypatch.setattr(exposure, "_route_files", lambda: [])
    exposure.clear_cache()
    tls._ctx_cache.clear()
    yield
    exposure.clear_cache()


class Running:
    def __init__(self, app="app.web:app"):
        self.port = free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error",
                             proxy_headers=False, server_header=False)
        self.server = oneport.Server(cfg)
        self.thread = threading.Thread(target=oneport.serve, args=(cfg, self.server),
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and not self.server.started:
            time.sleep(0.05)
        assert self.server.started
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive()

    def url(self, path="/api/tv/hello", scheme="http"):
        return f"{scheme}://127.0.0.1:{self.port}{path}"


async def echo(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = json.dumps({"client": list(scope["client"]), "scheme": scope["scheme"]})
    if scope["path"] == "/stream":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(5):
            await send({"type": "http.response.body", "body": b"x" * 100000,
                        "more_body": True})
        await send({"type": "http.response.body", "body": b""})
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body.encode()})


def served_fingerprint(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with ctx.wrap_socket(raw) as s:
            der = s.getpeercert(binary_form=True)
    return tls.fingerprint_of(x509.load_der_x509_certificate(der).public_key())


def test_away_on_serves_plain_and_tls_on_one_port():
    db.set_setting("away_mode", "1")
    with Running() as r:
        assert httpx.get(r.url()).status_code == 200
        got = httpx.get(r.url(scheme="https"), verify=False)
        assert got.status_code == 200
        assert got.headers["strict-transport-security"]
        assert served_fingerprint(r.port) == tls.ensure_own().fingerprint


def test_away_off_is_plain_http_only_as_before():
    with Running() as r:
        assert httpx.get(r.url()).status_code == 200
        with pytest.raises(httpx.HTTPError):
            httpx.get(r.url(scheme="https"), verify=False)
        assert httpx.get(r.url()).status_code == 200       # still alive after the refusal
    assert not (paths.data_dir() / "tls").exists()


def test_turning_the_switch_on_needs_no_restart():
    with Running() as r:
        with pytest.raises(httpx.HTTPError):
            httpx.get(r.url(scheme="https"), verify=False)
        db.set_setting("away_mode", "1")
        assert httpx.get(r.url(scheme="https"), verify=False).status_code == 200


def test_plain_http_from_outside_gets_a_308_to_https(monkeypatch):
    db.set_setting("away_mode", "1")
    monkeypatch.setattr(exposure, "is_home", lambda request: False)
    with Running() as r:
        got = httpx.post(r.url("/login?next=%2Fx"), follow_redirects=False)
        assert got.status_code == 308
        assert got.headers["location"] == f"https://127.0.0.1:{r.port}/login?next=%2Fx"
        assert httpx.get(r.url(scheme="https"), verify=False).status_code == 200


def test_plain_http_at_home_is_not_redirected():
    db.set_setting("away_mode", "1")
    with Running() as r:                 # loopback is home
        assert httpx.get(r.url(), follow_redirects=False).status_code == 200


def test_a_trusted_proxy_is_not_redirected(monkeypatch):
    db.set_setting("away_mode", "1")
    db.set_setting("trusted_proxies", "127.0.0.1")
    monkeypatch.setattr(exposure, "is_home", lambda request: False)
    with Running() as r:
        got = httpx.get(r.url(), follow_redirects=False,
                        headers={"X-Forwarded-For": "8.8.8.8"})
        assert got.status_code == 200


def test_the_client_address_is_the_real_one_over_both():
    db.set_setting("away_mode", "1")
    with Running(echo) as r:
        for scheme in ("http", "https"):
            with httpx.Client(verify=False) as c:
                got = c.get(r.url("/", scheme)).json()
                assert got["client"][0] == "127.0.0.1"
                assert got["scheme"] == scheme
        # The port is the client's own, so it is the peer, not a hop.
        s = socket.create_connection(("127.0.0.1", r.port))
        mine = s.getsockname()[1]
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        body = b""
        while chunk := s.recv(4096):
            body += chunk
        s.close()
        assert json.loads(body.split(b"\r\n\r\n", 1)[1])["client"][1] == mine


def test_keep_alive_and_streaming_work_over_tls():
    db.set_setting("away_mode", "1")
    with Running(echo) as r:
        with httpx.Client(verify=False) as c:
            assert c.get(r.url("/", "https")).status_code == 200
            big = c.get(r.url("/stream", "https"))
            assert len(big.content) == 500000
            assert c.get(r.url("/", "https")).status_code == 200      # same connection


def test_a_client_that_sends_nothing_does_not_stall_others(monkeypatch):
    monkeypatch.setattr(oneport, "FIRST_BYTE_SECONDS", 1)
    db.set_setting("away_mode", "1")
    with Running() as r:
        quiet = [socket.create_connection(("127.0.0.1", r.port)) for _ in range(20)]
        try:
            t0 = time.time()
            assert httpx.get(r.url(), timeout=3).status_code == 200
            assert httpx.get(r.url(scheme="https"), verify=False, timeout=3).status_code == 200
            assert time.time() - t0 < 1
            # After the wait they are dropped.
            time.sleep(1.5)
            quiet[0].settimeout(2)
            assert quiet[0].recv(10) == b""
        finally:
            for q in quiet:
                q.close()


def test_a_slow_first_byte_still_works():
    with Running() as r:
        s = socket.create_connection(("127.0.0.1", r.port))
        time.sleep(0.5)
        s.sendall(b"GET /api/tv/hello HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        s.close()
        assert data.startswith(b"HTTP/1.1 200")


def test_shutdown_is_graceful_with_an_open_connection():
    db.set_setting("away_mode", "1")
    r = Running()
    with r:
        c = httpx.Client(verify=False)
        assert c.get(r.url(scheme="https")).status_code == 200
    c.close()
    assert not r.thread.is_alive()


def test_the_healthcheck_url_keeps_working():
    # Docker's HEALTHCHECK is plain http on loopback, which is home.
    db.set_setting("away_mode", "1")
    with Running() as r:
        assert httpx.get(r.url("/healthz"), follow_redirects=False).status_code == 200
