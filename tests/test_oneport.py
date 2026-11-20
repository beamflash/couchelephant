"""One port, HTTP and HTTPS, on a real server."""
import ipaddress
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
    if scope["path"] in ("/stream", "/huge"):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(5 if scope["path"] == "/stream" else 150):
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
        assert got.headers["cache-control"] == "no-store"
        # A page load gets the kind a browser does not remember.
        for method in ("GET", "HEAD"):
            got = httpx.request(method, r.url("/login"), follow_redirects=False)
            assert got.status_code == 307
            assert got.headers["cache-control"] == "no-store"
            assert got.headers["location"] == f"https://127.0.0.1:{r.port}/login"
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


def test_100_silent_clients_do_not_hold_up_a_real_one_with_no_reader_loop(monkeypatch):
    # Windows' proactor loop has no add_reader; one watcher thread serves all.
    monkeypatch.setattr(oneport, "NO_READER", True)
    monkeypatch.setattr(oneport, "FIRST_BYTE_SECONDS", 2)
    db.set_setting("away_mode", "1")
    with Running() as r:
        quiet = [socket.create_connection(("127.0.0.1", r.port)) for _ in range(100)]
        try:
            time.sleep(0.3)
            t0 = time.time()
            assert httpx.get(r.url(), timeout=5).status_code == 200
            assert httpx.get(r.url(scheme="https"), verify=False, timeout=5).status_code == 200
            assert time.time() - t0 < 1
            # Their deadline counts from accept, and then they are closed.
            time.sleep(2.5)
            quiet[50].settimeout(2)
            assert quiet[50].recv(10) == b""
            assert threading.active_count() < 40
        finally:
            for q in quiet:
                q.close()


def test_a_bad_host_with_a_backslash_is_not_redirected(monkeypatch):
    db.set_setting("away_mode", "1")
    monkeypatch.setattr(exposure, "is_home", lambda request: False)
    with Running() as r:
        got = httpx.get(r.url("/x"), headers={"Host": "evil.example\\@a"},
                        follow_redirects=False)
        assert got.status_code not in (307, 308)


def test_windows_takes_the_port_exclusively():
    calls = []

    class Fake:
        def __init__(self, family=None):
            pass

        def setsockopt(self, level, opt, val):
            calls.append((opt, val))

        def bind(self, addr):
            calls.append(("bind", addr))

        def getsockname(self):
            return ("0.0.0.0", 1234)
    import socket as sk
    cfg = uvicorn.Config("app.web:app", host="0.0.0.0", port=1234)
    old = getattr(sk, "SO_EXCLUSIVEADDRUSE", None)
    sk.SO_EXCLUSIVEADDRUSE = -5
    try:
        oneport._listening_socket(cfg, windows=True, make=Fake)
    finally:
        if old is None:
            del sk.SO_EXCLUSIVEADDRUSE
    assert (-5, 1) in calls
    assert not any(c[0] == sk.SO_REUSEADDR for c in calls)
    assert ("bind", ("0.0.0.0", 1234)) in calls


def test_other_systems_keep_reuseaddr():
    cfg = uvicorn.Config("app.web:app", host="127.0.0.1", port=0)
    sock = oneport._listening_socket(cfg, windows=False)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    finally:
        sock.close()


def test_accept_errors_pause_only_when_out_of_descriptors_and_log_rarely(caplog, monkeypatch):
    import errno
    monkeypatch.setattr(oneport, "_last_accept_log", 0.0)
    clock = [1000.0]
    monkeypatch.setattr(oneport.time, "monotonic", lambda: clock[0])
    with caplog.at_level("WARNING", logger="couchelephant.oneport"):
        assert oneport._accept_failed(OSError(errno.ECONNABORTED, "x")) is False
        assert oneport._accept_failed(OSError(errno.EMFILE, "x")) is True
        assert oneport._accept_failed(OSError(errno.ENFILE, "x")) is True
        assert oneport._accept_failed(OSError(errno.ENOBUFS, "x")) is True
        assert len(caplog.records) == 1
        clock[0] += 11
        oneport._accept_failed(OSError(errno.EMFILE, "x"))
        assert len(caplog.records) == 2


def test_our_own_tls_failure_is_warned_once(caplog, monkeypatch):
    monkeypatch.setattr(oneport, "_tls_warned", False)

    def broken():
        raise ssl.SSLError("bad context")
    monkeypatch.setattr(tls, "context", broken)
    db.set_setting("away_mode", "1")
    with caplog.at_level("DEBUG", logger="couchelephant.oneport"), Running() as r:
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", r.port))
            s.sendall(b"\x16\x03\x01\x00\x05hello")
            s.settimeout(2)
            try:
                s.recv(10)
            except OSError:
                pass
            s.close()
        time.sleep(0.3)
    warns = [x for x in caplog.records if x.levelname == "WARNING"]
    assert len(warns) == 1


def test_the_context_is_kept_until_the_files_change():
    db.set_setting("away_mode", "1")
    a = tls.context()
    assert tls.context() is a
    own = tls.ensure_own()
    own.cert_path.write_bytes(own.cert_path.read_bytes() + b"\n")   # touched
    assert tls.context() is not a


def test_tls_one_two_ciphers_are_the_modern_ones():
    db.set_setting("away_mode", "1")
    ctx = tls.context()
    names = [c["name"] for c in ctx.get_ciphers() if c["protocol"] != "TLSv1.3"]
    assert names and all(n.startswith("ECDHE") for n in names)
    assert not any("CBC" in n or "RC4" in n for n in names)


# ---------- slow clients after the hand-over, and the connection cap ----------

def test_headers_that_trickle_are_cut_off_after_the_deadline(monkeypatch):
    monkeypatch.setattr(oneport, "HEADER_SECONDS", 1)
    with Running() as r:
        s = socket.create_connection(("127.0.0.1", r.port))
        try:
            s.sendall(b"GET /api/tv/hello HTTP/1.1\r\nHost: x\r\n")
            time.sleep(0.5)
            s.sendall(b"X-A: 1\r\n")                     # still no blank line
            time.sleep(1.5)
            s.settimeout(3)
            try:
                got = s.recv(100)
            except ConnectionError:
                got = b""
            assert got == b""                            # closed, no answer
        finally:
            s.close()


def test_a_slow_but_finishing_request_and_keep_alive_still_work(monkeypatch):
    monkeypatch.setattr(oneport, "HEADER_SECONDS", 2)
    with Running() as r:
        with httpx.Client() as c:
            assert c.get(r.url()).status_code == 200
            time.sleep(2.5)                              # idle, not mid-header
            assert c.get(r.url()).status_code == 200


def _outsiders(monkeypatch):
    """127.0.0.1 counts as outside here (as a Docker gateway would); other
    loopback addresses stay home."""
    monkeypatch.setattr(exposure, "_never_home",
                        lambda: ({ipaddress.ip_address("127.0.0.1")}, []))


def _dial(port, source=None):
    return socket.create_connection(("127.0.0.1", port),
                                    source_address=(source, 0) if source else None)


def test_outsiders_past_the_cap_are_refused_at_once(monkeypatch):
    _outsiders(monkeypatch)
    monkeypatch.setattr(oneport, "MAX_CONNECTIONS", 5)
    with Running() as r:
        held = [_dial(r.port) for _ in range(5)]
        try:
            time.sleep(0.3)
            extra = _dial(r.port)
            extra.settimeout(2)
            assert extra.recv(10) == b""                 # closed by the server
            extra.close()
        finally:
            for h in held:
                h.close()
        time.sleep(0.3)
        assert httpx.get(r.url(), timeout=3).status_code == 200      # room again


def test_a_home_peer_still_connects_when_outsiders_fill_their_share(monkeypatch):
    _outsiders(monkeypatch)
    monkeypatch.setattr(oneport, "MAX_CONNECTIONS", 5)
    with Running() as r:
        held = [_dial(r.port) for _ in range(5)]
        try:
            time.sleep(0.3)
            home = _dial(r.port, "127.0.0.2")
            home.settimeout(5)
            home.sendall(b"GET /api/tv/hello HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert home.recv(100).startswith(b"HTTP/1.1 ")
            home.close()
        finally:
            for h in held:
                h.close()


def _plain_outsider(monkeypatch):
    """127.0.0.1 is an outsider on the internet, not a Docker gateway."""
    monkeypatch.setattr(exposure, "peer_is_home", lambda h: h != "127.0.0.1")
    monkeypatch.setattr(exposure, "peer_is_bridge", lambda h: False)


def test_a_bridge_gateway_is_not_held_to_sixteen(monkeypatch):
    # Every visitor of a bridge container comes from the gateway.
    _outsiders(monkeypatch)
    monkeypatch.setattr(oneport, "MAX_CONNECTIONS", 200)
    with Running() as r:
        held = [_dial(r.port) for _ in range(20)]
        try:
            time.sleep(0.3)
            assert httpx.get(r.url(), timeout=3).status_code == 200
        finally:
            for h in held:
                h.close()


def test_one_outsider_address_cannot_pass_sixteen(monkeypatch):
    _plain_outsider(monkeypatch)
    monkeypatch.setattr(oneport, "MAX_CONNECTIONS", 200)
    with Running() as r:
        held = [_dial(r.port) for _ in range(16)]
        try:
            time.sleep(0.3)
            extra = _dial(r.port)
            extra.settimeout(2)
            assert extra.recv(10) == b""                 # that address is full
            extra.close()
        finally:
            for h in held:
                h.close()


def test_an_ipv6_outsider_is_counted_by_its_sixty_four():
    a = oneport._peer_key("2001:db8:1:2:aaaa::1")
    assert a == oneport._peer_key("2001:db8:1:2:bbbb::9")
    assert a != oneport._peer_key("2001:db8:1:3::1")
    assert oneport._peer_key("192.0.2.7") == "192.0.2.7"


def test_a_pipelined_request_that_streams_past_the_deadline_is_not_closed(monkeypatch):
    monkeypatch.setattr(oneport, "HEADER_SECONDS", 1)
    with Running(echo) as r:
        s = _dial(r.port)
        try:
            one = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
            s.sendall(one + b"GET /huge HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            s.settimeout(10)
            time.sleep(2.5)              # past the deadline, reader not draining
            got = b""
            while chunk := s.recv(65536):
                got += chunk
            assert len(got) > 15_000_000   # the whole stream arrived
        finally:
            s.close()


def test_the_cap_sits_below_the_descriptor_limit():
    assert oneport.connection_cap(256) < 256 // 2 + 1
    assert oneport.connection_cap(1024) > 100
    assert oneport.connection_cap(None) > 0
