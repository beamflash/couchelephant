"""A real certificate for a domain. The parts that need no network run
always; the order itself runs against Pebble, Let's Encrypt's test server,
and is skipped with a reason when there is no Pebble to talk to."""
import datetime
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import types
import uuid
from pathlib import Path

import pytest
import requests
import uvicorn
from acme import errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import acme_tls, announce, db, exposure, oneport, paths, tls

HERE = Path(__file__).parent


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
    tls.challenge_clear()
    acme_tls._wanted.clear()
    yield
    tls.challenge_clear()
    exposure.clear_cache()


def make_real(name, days=90, sans=None):
    """A stand-in for the certificate Let's Encrypt would give."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    n = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(n).issuer_name(n)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(s) for s in (sans or [name])]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = acme_tls._files(name)
    tls._write(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()), 0o600)
    tls._write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert


# ---------- the name ----------

@pytest.mark.parametrize("text,out", [
    ("Tv.Example.com", "tv.example.com"),
    ("  tv.example.com.  ", "tv.example.com"),
    ("bücher.example.com", "xn--bcher-kva.example.com"),
])
def test_domain_is_cleaned(text, out):
    assert acme_tls.clean_domain(text) == out


@pytest.mark.parametrize("text,words", [
    ("tv.local", ".local"), ("tv.home.arpa", ".home.arpa"), ("tv.lan", ".lan"),
    ("tv.internal", ".internal"), ("tv.example.123", "not a number"),
    ("tv\u200b.example.com", "invisible"), ("tv\u2060.example.com", "invisible"),
    ("\uff54\uff56.example.com", "plain letters"),
])
def test_names_that_cannot_work_are_refused_with_the_reason(text, words):
    with pytest.raises(ValueError) as e:
        acme_tls.clean_domain(text)
    assert words in str(e.value)


@pytest.mark.parametrize("text", ["", "localhost", "a b.com", "1.2.3.4", "-x.example.com",
                                  "x..com", "https://x.com", "x/y.com", "a" * 64 + ".com"])
def test_bad_domain_is_refused(text):
    with pytest.raises(ValueError):
        acme_tls.clean_domain(text)


# ---------- one order at a time ----------

def test_two_quick_requests_run_one_order(monkeypatch):
    calls, gate = [], threading.Event()

    def slow(name):
        calls.append(name)
        gate.wait(5)
    monkeypatch.setattr(acme_tls, "_run", slow)
    a = threading.Thread(target=acme_tls.request, args=("tv.example.com",))
    a.start()
    while not calls:
        time.sleep(0.01)
    acme_tls.request("tv.example.com")         # returns at once, no second order
    assert calls == ["tv.example.com"]
    gate.set()
    a.join(5)
    assert calls == ["tv.example.com"]
    assert not acme_tls._lock.locked()


def test_a_different_name_asked_meanwhile_is_ordered_next(monkeypatch):
    calls, gate = [], threading.Event()

    def slow(name):
        calls.append(name)
        if len(calls) == 1:
            gate.wait(5)
    monkeypatch.setattr(acme_tls, "_run", slow)
    a = threading.Thread(target=acme_tls.request, args=("one.example.com",))
    a.start()
    while not calls:
        time.sleep(0.01)
    acme_tls.request("two.example.com")
    gate.set()
    a.join(5)
    assert calls == ["one.example.com", "two.example.com"]


# ---------- renewal ----------

def _renew_setup(monkeypatch, days):
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    if days is not None:
        make_real("tv.example.com", days)
    calls = []
    monkeypatch.setattr(acme_tls, "request", calls.append)
    return calls


@pytest.mark.parametrize("days,renews", [(89, False), (31, False), (29, True),
                                         (2, True), (None, True)])
def test_renewal_is_thirty_days_before_the_end(monkeypatch, days, renews):
    calls = _renew_setup(monkeypatch, days)
    acme_tls.renew_if_due()
    assert calls == (["tv.example.com"] if renews else [])


def test_no_renewal_without_a_domain_or_with_away_off(monkeypatch):
    calls = _renew_setup(monkeypatch, 2)
    db.set_setting("away_mode", "0")
    acme_tls.renew_if_due()
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "")
    acme_tls.renew_if_due()
    assert calls == []


def test_the_loop_renews_once_a_day(monkeypatch):
    calls = []
    monkeypatch.setattr(acme_tls, "renew_if_due", lambda: calls.append(1))
    monkeypatch.setattr(acme_tls, "_last_renew", [0.0])
    acme_tls.renew_daily()
    acme_tls.renew_daily()
    assert calls == [1]
    acme_tls._last_renew[0] -= acme_tls.DAY + 1
    acme_tls.renew_daily()
    assert calls == [1, 1]


def test_the_test_environment_starts_no_loops():
    assert os.environ.get("COUCHELEPHANT_NO_SYNC_LOOP") != "0"
    src = (HERE.parent / "app" / "web.py").read_text()
    early = src.index('COUCHELEPHANT_NO_SYNC_LOOP") == "1"')
    assert src.index("announce_loop())") > early      # the loops start past that return


# ---------- what the page says ----------

def test_status_messages():
    assert acme_tls.status()["state"] == "none"
    db.set_setting("away_domain", "tv.example.com")
    assert acme_tls.status()["say"] == "No certificate yet. Press Try again."
    db.set_setting("away_domain_error", "It broke.")
    st = acme_tls.status()
    assert (st["state"], st["say"]) == ("error", "It broke.")
    make_real("tv.example.com", 90)
    until = (datetime.datetime.now(datetime.UTC)
             + datetime.timedelta(days=90)).date().isoformat()
    assert acme_tls.status()["say"] == f"Certificate valid until {until}"
    with acme_tls._lock:
        assert acme_tls.status()["say"] == "Getting a certificate..."


def _fail(typ):
    err = messages.Error(typ=typ, detail="x")
    authz = types.SimpleNamespace(body=types.SimpleNamespace(
        identifier=types.SimpleNamespace(value="tv.example.com"),
        challenges=[types.SimpleNamespace(error=err, chall=types.SimpleNamespace(typ="x"))]))
    return errors.ValidationError([authz])


@pytest.mark.parametrize("exc,words", [
    (_fail("urn:ietf:params:acme:error:connection"),
     "Let's Encrypt could not reach this server on port 443; forward port 443 on your "
     "router to port 8099 on this computer"),
    (_fail("urn:ietf:params:acme:error:dns"), "does not point to this server"),
    (messages.Error(typ="urn:ietf:params:acme:error:rateLimited", detail="x"),
     "too many certificates"),
    (errors.TimeoutError(), "took too long"),
    (requests.ConnectionError("no route"), "could not reach Let's Encrypt"),
    (ValueError("anything else"), "could not give a certificate"),
])
def test_errors_are_plain(monkeypatch, exc, words):
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8099")
    say = acme_tls.explain(exc, "tv.example.com")
    assert words in say
    assert "Traceback" not in say and "Error(" not in say


def test_a_failed_order_keeps_the_own_certificate_and_says_why(monkeypatch):
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8099")

    def boom(name):
        raise _fail("urn:ietf:params:acme:error:connection")
    monkeypatch.setattr(acme_tls, "_order", boom)
    acme_tls.request("tv.example.com")
    assert "port 443 on your router to port 8099" in acme_tls.status()["say"]
    assert tls.context() is not None
    assert acme_tls.real_cert("tv.example.com") is None


def test_an_order_needs_away_on():
    db.set_setting("away_domain", "tv.example.com")
    acme_tls.request("tv.example.com")
    assert acme_tls.status()["say"].startswith("Turn on Use away from home")


# ---------- the pairing rule ----------

def _req(scheme, host):
    return types.SimpleNamespace(headers={"host": host},
                                 url=types.SimpleNamespace(scheme=scheme))


def test_outside_pairing_needs_https_a_real_certificate_and_the_name():
    from app.web import pairing_open_to_outsiders as ok
    db.set_setting("away_domain", "tv.example.com")
    assert not ok(_req("https", "tv.example.com"))           # no certificate yet
    make_real("tv.example.com", 90)
    assert ok(_req("https", "tv.example.com"))
    assert ok(_req("https", "TV.example.com:443"))
    assert not ok(_req("http", "tv.example.com"))            # not over TLS
    assert not ok(_req("https", "192.168.1.20:8099"))        # not by that name
    assert not ok(_req("https", "other.example.com"))
    assert not ok(_req("https", "[::1]:8099"))
    db.set_setting("away_domain", "")
    assert not ok(_req("https", "tv.example.com"))


def test_an_expired_or_wrong_certificate_does_not_open_pairing():
    from app.web import pairing_open_to_outsiders as ok
    db.set_setting("away_domain", "tv.example.com")
    make_real("tv.example.com", -1)
    assert not ok(_req("https", "tv.example.com"))
    make_real("tv.example.com", 90, sans=["other.example.com"])
    assert not ok(_req("https", "tv.example.com"))


# ---------- the settings route and the self-check ----------

def _client():
    from app.web import app

    async def home(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.5", 4321)}
        await app(scope, receive, send)
    return TestClient(home, base_url="http://testserver",
                      headers={"Origin": "http://testserver"})


def test_the_domain_route(monkeypatch):
    started = []
    monkeypatch.setattr(acme_tls, "start_in_background", started.append)
    c = _client()
    r = c.post("/settings/away/domain", data={"domain": "tv.example.com"}).json()
    assert r["ok"] is False and "Use away from home" in r["error"]     # switch is off
    db.set_setting("away_mode", "1")
    r = c.post("/settings/away/domain", data={"domain": "not a name"}).json()
    assert r["ok"] is False and "valid domain" in r["error"]
    assert started == []
    r = c.post("/settings/away/domain", data={"domain": " TV.Example.com "}).json()
    assert r["ok"] is True
    assert db.get_setting("away_domain") == "tv.example.com"
    assert started == ["tv.example.com"]
    r = c.post("/settings/away/domain", data={"domain": ""}).json()     # back to own
    assert r["ok"] is True and db.get_setting("away_domain") == ""
    assert c.get("/api/away/domain").json()["state"] == "none"


def _encryption(c):
    items = c.get("/api/away/check").json()["items"]
    return next(i for i in items if i["name"] == "Encryption")


def test_the_self_check_says_about_the_certificate():
    db.set_setting("away_mode", "1")
    c = _client()
    assert "not encrypted" in _encryption(c)["say"]
    db.set_setting("away_domain", "tv.example.com")
    db.set_setting("away_domain_error", "Let's Encrypt could not reach this server.")
    e = _encryption(c)
    assert e["ok"] is False and "could not reach this server" in e["say"]
    assert "browsers will warn" in e["say"]
    make_real("tv.example.com", 90)
    e = _encryption(c)
    assert e["ok"] is True and "valid until" in e["say"] and "tv.example.com" in e["say"]


# ---------- which certificate the port serves ----------

class Running:
    def __init__(self, port=None, host="127.0.0.1"):
        self.port = port or free_port()
        cfg = uvicorn.Config("app.web:app", host=host, port=self.port, log_level="error",
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


def peer(port, sni=None, alpn=None):
    """The certificate the port shows for this name, and the protocol chosen."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if alpn:
        ctx.set_alpn_protocols(alpn)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=sni) as s:
            der = s.getpeercert(binary_form=True)
            return x509.load_der_x509_certificate(der), s.selected_alpn_protocol()


def sans(cert):
    return cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)


def test_the_real_certificate_for_its_name_and_the_own_for_any_other():
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    real = make_real("tv.example.com", 90)
    own = tls.ensure_own()
    own_der = x509.load_pem_x509_certificate(own.cert_path.read_bytes())
    with Running() as srv:
        got, _ = peer(srv.port, "tv.example.com")
        assert got.serial_number == real.serial_number
        got, _ = peer(srv.port, "TV.EXAMPLE.COM")
        assert got.serial_number == real.serial_number
        for sni in ("other.example.com", "192.168.1.20", None):
            got, _ = peer(srv.port, sni)
            assert got.serial_number == own_der.serial_number


def test_a_new_real_certificate_is_served_without_a_restart():
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    first = make_real("tv.example.com", 60)
    with Running() as srv:
        assert peer(srv.port, "tv.example.com")[0].serial_number == first.serial_number
        second = make_real("tv.example.com", 90)
        assert peer(srv.port, "tv.example.com")[0].serial_number == second.serial_number


def test_with_no_real_certificate_every_name_gets_the_own_one():
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    own = x509.load_pem_x509_certificate(tls.ensure_own().cert_path.read_bytes())
    with Running() as srv:
        assert peer(srv.port, "tv.example.com")[0].serial_number == own.serial_number


def test_the_challenge_certificate_answers_acme_tls_only_during_an_order():
    from app import acme_tls as a
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    with Running() as srv:
        cert_pem, key_pem = a.challenge_cert("tv.example.com", "tok.thumb")
        tls.challenge_set("tv.example.com", cert_pem, key_pem)
        got, proto = peer(srv.port, "tv.example.com", alpn=["acme-tls/1"])
        assert proto == "acme-tls/1" and sans(got) == ["tv.example.com"]
        ext = got.extensions.get_extension_for_oid(
            x509.ObjectIdentifier("1.3.6.1.5.5.7.1.31"))
        assert ext.critical and ext.value.value[2:] == \
            __import__("hashlib").sha256(b"tok.thumb").digest()
        # only a client that offers acme-tls/1 gets the challenge (RFC 8737)
        own_serial = x509.load_pem_x509_certificate(
            tls.ensure_own().cert_path.read_bytes()).serial_number
        for alpn in (None, ["h2", "http/1.1"], ["http/1.1"]):
            got, proto = peer(srv.port, "tv.example.com", alpn=alpn)
            assert got.serial_number == own_serial and proto != "acme-tls/1"
        # another name still gets the own certificate, with no acme protocol
        got, proto = peer(srv.port, "other.example.com", alpn=["acme-tls/1"])
        assert proto is None and "tv.example.com" not in sans(got)
        tls.challenge_clear()
        _, proto = peer(srv.port, "tv.example.com", alpn=["acme-tls/1"])
        assert proto is None
    assert not list((paths.data_dir() / "tls").glob("challenge.*"))


# ---------- the ClientHello ----------

def a_hello(sni="tv.example.com", alpn=("acme-tls/1",), version=None):
    """A real ClientHello, the bytes a TLS client sends first."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if alpn:
        ctx.set_alpn_protocols(list(alpn))
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=sni)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


def test_the_parser_reads_name_and_protocols():
    assert tls.parse_client_hello(a_hello()) == ("tv.example.com", ["acme-tls/1"])
    assert tls.parse_client_hello(a_hello(alpn=("h2", "http/1.1"))) == \
        ("tv.example.com", ["h2", "http/1.1"])
    assert tls.parse_client_hello(a_hello(alpn=None)) == ("tv.example.com", [])
    assert tls.parse_client_hello(a_hello(sni=None)) == (None, ["acme-tls/1"])


def test_the_parser_never_crashes_on_junk():
    import random
    good = a_hello()
    for n in range(len(good)):                       # every truncation
        assert tls.parse_client_hello(good[:n]) is None
    rng = random.Random(7)
    for _ in range(3000):
        junk = bytearray(good)
        for _ in range(rng.randint(1, 6)):           # a few flipped bytes
            junk[rng.randrange(len(junk))] = rng.randrange(256)
        tls.parse_client_hello(bytes(junk))          # None or a tuple, no exception
    for _ in range(500):
        tls.parse_client_hello(bytes(rng.randrange(256) for _ in range(rng.randint(0, 400))))
    assert tls.parse_client_hello(good + b"trailing") == ("tv.example.com", ["acme-tls/1"])
    # lengths that lie
    lie = bytearray(good)
    lie[3:5] = (len(good) + 500).to_bytes(2, "big")
    assert tls.parse_client_hello(bytes(lie)) is None


def _split_handshake(port, hello_chunks, alpn, sni, pause=0.3):
    """A client that sends its ClientHello in pieces, slowly."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(alpn)
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=sni)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        try:
            obj.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = out.read()
        pos = 0
        for n in hello_chunks:
            raw.sendall(hello[pos:pos + n])
            pos += n
            time.sleep(pause)
        raw.sendall(hello[pos:])
        while True:
            try:
                obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                data = out.read()
                if data:
                    raw.sendall(data)
                chunk = raw.recv(65536)
                assert chunk, "the server closed the connection"
                inc.write(chunk)
        return (x509.load_der_x509_certificate(obj.getpeercert(binary_form=True)),
                obj.selected_alpn_protocol())


def test_a_hello_sent_in_pieces_still_gets_the_challenge():
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    with Running() as srv:
        cert_pem, key_pem = acme_tls.challenge_cert("tv.example.com", "tok.thumb")
        tls.challenge_set("tv.example.com", cert_pem, key_pem)
        got, proto = _split_handshake(srv.port, [3, 2, 40], ["acme-tls/1"],
                                      "tv.example.com")
        assert proto == "acme-tls/1" and sans(got) == ["tv.example.com"]
        got, proto = _split_handshake(srv.port, [3, 2, 40], ["h2"], "tv.example.com")
        assert proto != "acme-tls/1" and "tv.example.com" not in sans(got)


def test_a_broken_hello_does_not_hurt_the_port():
    db.set_setting("away_mode", "1")
    with Running() as srv:
        good = a_hello()
        evil = [good[:7], b"\x16\x03\x01\xff\xff" + b"x" * 100,    # 64 KB claimed, then nothing
                b"\x16\x03\x01\x00\x10" + b"\x00" * 16, b"\x16", b"\x16\x03"]
        for junk in evil:
            with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as c:
                c.sendall(junk)
                c.shutdown(socket.SHUT_WR)
                try:
                    c.recv(100)
                except OSError:
                    pass
        assert peer(srv.port, "other.example.com")[0] is not None


# ---------- the queue, the throttle, small guards ----------

def test_a_name_queued_during_the_hand_back_is_not_lost(monkeypatch):
    calls = []
    monkeypatch.setattr(acme_tls, "_run", calls.append)

    class Slow(list):
        def __len__(self):                   # the moment the first order checks the queue
            time.sleep(0.4)
            return list.__len__(self)
    monkeypatch.setattr(acme_tls, "_wanted", Slow())
    a = threading.Thread(target=acme_tls.request, args=("one.example.com",))
    a.start()
    time.sleep(0.15)
    acme_tls.request("two.example.com")      # arrives while the first is finishing
    a.join(10)
    for _ in range(100):
        if "two.example.com" in calls and not acme_tls._lock.locked():
            break
        time.sleep(0.05)
    assert calls == ["one.example.com", "two.example.com"]


def _count_orders(monkeypatch):
    calls = []
    monkeypatch.setattr(acme_tls, "_order", calls.append)
    db.set_setting("away_mode", "1")
    return calls


def test_no_order_when_the_certificate_is_good(monkeypatch):
    calls = _count_orders(monkeypatch)
    db.set_setting("away_domain", "tv.example.com")
    make_real("tv.example.com", 80)
    acme_tls.request("tv.example.com")
    assert calls == []
    make_real("tv.example.com", 10)               # due: it orders
    acme_tls.request("tv.example.com")
    assert calls == ["tv.example.com"]


def test_one_attempt_an_hour_unless_the_name_changed(monkeypatch):
    calls = _count_orders(monkeypatch)
    acme_tls.request("tv.example.com")
    acme_tls.request("tv.example.com")            # a restart leaves only the database
    assert calls == ["tv.example.com"]
    assert 0 < acme_tls.too_soon("tv.example.com") <= 3600
    assert acme_tls.too_soon("other.example.com") == 0
    acme_tls.request("other.example.com")         # a new name goes at once
    assert calls == ["tv.example.com", "other.example.com"]
    db.set_setting("away_domain_last_try", str(int(time.time()) - 3601))
    acme_tls.request("other.example.com")
    assert calls[-1] == "other.example.com" and len(calls) == 3


def test_the_save_says_how_long_to_wait(monkeypatch):
    _count_orders(monkeypatch)
    started = []
    monkeypatch.setattr(acme_tls, "start_in_background", started.append)
    acme_tls.request("tv.example.com")
    r = _client().post("/settings/away/domain", data={"domain": "tv.example.com"}).json()
    assert r["ok"] is False and "minute" in r["error"] and started == []
    r = _client().post("/settings/away/domain", data={"domain": "new.example.com"}).json()
    assert r["ok"] is True and started == ["new.example.com"]


def test_one_error_does_not_end_the_loop(monkeypatch):
    from app import web
    seen = []

    def boom_check():
        seen.append("check")
        raise RuntimeError("no network")

    def boom_renew():
        seen.append("renew")
        raise RuntimeError("no disk")
    monkeypatch.setattr(announce, "check", boom_check)
    monkeypatch.setattr(acme_tls, "renew_daily", boom_renew)
    web._announce_tick()                          # must not raise
    assert seen == ["check", "renew"]


def test_an_unusable_account_key_is_replaced():
    from cryptography.hazmat.primitives.asymmetric import rsa
    path = paths.data_dir() / "tls" / "acme-account.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = ec.generate_private_key(ec.SECP256R1()).private_bytes(      # needs a password
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"x"))
    rsa_key = rsa.generate_private_key(65537, 2048).private_bytes(   # not an EC key
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    for bad in (enc, rsa_key, b"junk", b""):
        path.write_bytes(bad)
        jwk = acme_tls._account_key()
        assert jwk.key.curve.name == "secp256r1"
        assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY")


def test_the_hot_path_parses_no_pem(monkeypatch):
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", "tv.example.com")
    make_real("tv.example.com", 90)
    calls = []
    real_load = tls._load
    monkeypatch.setattr(tls, "_load", lambda *a: calls.append(1) or real_load(*a))
    for _ in range(50):
        assert acme_tls.trusted_for("tv.example.com")
    assert len(calls) == 1
    make_real("tv.example.com", 80)                # new files: parsed again
    assert acme_tls.trusted_for("tv.example.com")
    assert len(calls) == 2
    calls.clear()
    tls.context()
    for _ in range(50):
        tls.context()
    assert len(calls) <= 1


def test_the_pebble_image_is_pinned():
    ci = (HERE.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert "pebble:latest" not in ci and "ghcr.io/letsencrypt/pebble:" in ci
    assert ":latest" not in IMAGE


# ---------- the order itself, against Pebble ----------
#
# Pebble is Let's Encrypt's test server. With PEBBLE_VA_ALWAYS_VALID=0 it
# really connects to the name on its TLS-ALPN port and checks the answer.
# Two ways to have one:
#   - PEBBLE_DIRECTORY, PEBBLE_TLS_PORT, PEBBLE_DOMAIN set (CI: a service
#     container, and Pebble reaches this job by its own host name);
#   - docker on this machine (plain, or through `sg docker`), which this file
#     starts and removes again.

MINICA = str(HERE / "pebble.minica.pem")
IMAGE = "ghcr.io/letsencrypt/pebble:2.10.3"


def _docker(*args, timeout=120):
    for pre in (["docker"], ["sg", "docker", "-c"]):
        if pre[0] == "sg":
            if not shutil.which("sg"):
                return None
            cmd = pre + [" ".join(["docker"] + [_q(a) for a in args])]
        else:
            if not shutil.which("docker"):
                continue
            cmd = pre + list(args)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 or pre[0] == "sg":
            return out
        if "permission denied" not in out.stderr.lower():
            return out
    return None


def _q(a):
    import shlex
    return shlex.quote(str(a))


def _wait_directory(url, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if requests.get(url, verify=MINICA, timeout=3).ok:
                return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def pebble(tmp_path_factory):
    env_dir = os.environ.get("PEBBLE_DIRECTORY")
    if env_dir:
        if not _wait_directory(env_dir):
            pytest.skip(f"PEBBLE_DIRECTORY {env_dir} does not answer")
        yield {"directory": env_dir,
               "tls_port": int(os.environ.get("PEBBLE_TLS_PORT", "5001")),
               "domain": os.environ.get("PEBBLE_DOMAIN", "test.local")}
        return
    probe = _docker("image", "inspect", IMAGE)
    if probe is None or probe.returncode != 0:
        pytest.skip("no Pebble: set PEBBLE_DIRECTORY, or have docker with "
                    f"{IMAGE} pulled")
    work = tmp_path_factory.mktemp("pebble")
    acme_port, mgmt_port, http_port, tls_port = (free_port() for _ in range(4))
    domain = "test.local"
    cfg = json.loads((HERE / "pebble-config.json").read_text())
    cfg["pebble"].update({"listenAddress": f"127.0.0.1:{acme_port}",
                          "managementListenAddress": f"127.0.0.1:{mgmt_port}",
                          "httpPort": http_port, "tlsPort": tls_port})
    (work / "pebble-config.json").write_text(json.dumps(cfg))
    name = "ce-pebble-" + uuid.uuid4().hex[:8]
    run = _docker("run", "-d", "--name", name, "--network", "host",
                  "--add-host", f"{domain}:127.0.0.1",
                  "-e", "PEBBLE_VA_ALWAYS_VALID=0", "-e", "PEBBLE_VA_NOSLEEP=1",
                  "-v", f"{work}:/cfg:ro", IMAGE, "-config", "/cfg/pebble-config.json")
    try:
        if run is None or run.returncode != 0:
            pytest.skip("docker could not start Pebble: "
                        + ((run.stderr if run else "no docker").strip()[:200]))
        url = f"https://localhost:{acme_port}/dir"
        if not _wait_directory(url):
            logs = _docker("logs", name)
            pytest.skip("Pebble did not come up: " + (logs.stderr[-300:] if logs else ""))
        yield {"directory": url, "tls_port": tls_port, "domain": domain}
    finally:
        _docker("rm", "-f", name)


@pytest.fixture
def at_pebble(pebble, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_ACME_DIRECTORY", pebble["directory"])
    monkeypatch.setenv("COUCHELEPHANT_ACME_CA", MINICA)
    db.set_setting("away_mode", "1")
    db.set_setting("away_domain", pebble["domain"])
    return pebble


def test_pebble_order_fails_plainly_when_nothing_answers_on_the_port(at_pebble):
    if _port_open(at_pebble["tls_port"]):
        pytest.skip("something already listens on Pebble's TLS port")
    acme_tls.request(at_pebble["domain"])
    st = acme_tls.status()
    assert st["state"] == "error"
    assert "forward port 443 on your router" in st["say"], st["say"]
    assert acme_tls.real_cert(at_pebble["domain"]) is None
    assert tls.context() is not None            # the own certificate is untouched


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_pebble_order_completes_on_the_one_port(at_pebble):
    name = at_pebble["domain"]
    own = x509.load_pem_x509_certificate(tls.ensure_own().cert_path.read_bytes())
    with Running(port=at_pebble["tls_port"], host="0.0.0.0") as srv:
        # two quick calls, one order
        t = threading.Thread(target=acme_tls.request, args=(name,))
        t.start()
        time.sleep(0.2)
        acme_tls.request(name)
        t.join(120)
        assert not t.is_alive()
        st = acme_tls.status()
        assert st["state"] == "ok", st
        got, _ = peer(srv.port, name)
        assert got.serial_number != own.serial_number
        assert name in sans(got)
        assert "Pebble" in got.issuer.rfc4514_string()
        got, _ = peer(srv.port, "other.example.com")
        assert got.serial_number == own.serial_number
        assert not list((paths.data_dir() / "tls").glob("challenge.*"))
        key = paths.data_dir() / "tls" / f"{name}.key.pem"
        assert key.stat().st_mode & 0o777 == 0o600
        assert (paths.data_dir() / "tls" / "acme-account.pem").stat().st_mode & 0o777 == 0o600
        # the second order reuses the account
        first = acme_tls._account_key().thumbprint()
        acme_tls.request(name)
        assert acme_tls._account_key().thumbprint() == first
        assert acme_tls.status()["state"] == "ok"
