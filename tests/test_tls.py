"""The own certificate: made once, ten years, the key's fingerprint."""
import base64
import datetime
import hashlib
import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import announce, db, exposure, paths, tls


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


def test_ensure_own_makes_the_files_once():
    a = tls.ensure_own()
    assert a.cert_path == tmp_dir() / "tls" / "cert.pem"
    mtime = a.cert_path.stat().st_mtime_ns
    b = tls.ensure_own()
    assert b == a
    assert b.cert_path.stat().st_mtime_ns == mtime


def tmp_dir():
    return paths.data_dir()


def test_the_key_file_is_private():
    own = tls.ensure_own()
    assert own.key_path.stat().st_mode & 0o077 == 0


def test_certificate_is_p256_ten_years_and_names_the_host_and_lan():
    own = tls.ensure_own()
    cert = x509.load_pem_x509_certificate(own.cert_path.read_bytes())
    assert isinstance(cert.public_key().curve, ec.SECP256R1)
    # cryptography before 42 has only the naive names.
    after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
        tzinfo=datetime.UTC)
    before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(
        tzinfo=datetime.UTC)
    span = after - before
    assert 3650 <= span.days <= 3655
    assert after > datetime.datetime.now(
        datetime.UTC) + datetime.timedelta(days=3640)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    dns = set(san.get_values_for_type(x509.DNSName))
    assert {"192.168.1.20", "127.0.0.1"} <= ips
    assert socket.gethostname().split(".")[0] in dns
    assert announce.host_label() + ".local" in dns


def test_fingerprint_is_base64url_sha256_of_the_spki():
    own = tls.ensure_own()
    cert = x509.load_pem_x509_certificate(own.cert_path.read_bytes())
    der = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    want = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert own.fingerprint == want
    assert "=" not in want and len(want) == 43


def test_a_new_data_folder_makes_a_new_key(monkeypatch, tmp_path_factory):
    first = tls.ensure_own().fingerprint
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path_factory.mktemp("other")))
    assert tls.ensure_own().fingerprint != first


def test_context_is_none_while_away_is_off_and_made_when_on():
    assert tls.context() is None
    db.set_setting("away_mode", "1")
    assert tls.context() is not None
    db.set_setting("away_mode", "0")
    assert tls.context() is None


def test_txt_carries_tls_and_fp_only_when_away_is_on():
    t = announce.txt()
    assert t["tls"] == "0" and "fp" not in t
    db.set_setting("away_mode", "1")
    t = announce.txt()
    assert t["tls"] == "1"
    assert t["fp"] == tls.ensure_own().fingerprint


def _home_client():
    from fastapi.testclient import TestClient

    from app.web import app

    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.5", 4321)}
        await app(scope, receive, send)
    c = TestClient(shim, base_url="http://testserver")
    c.headers.update({"Origin": "http://testserver"})
    return c


def test_the_switch_refreshes_the_announcement_both_ways(monkeypatch):
    from app.routes import settings
    calls = []
    monkeypatch.setattr(announce, "refresh", lambda retry=False: calls.append(retry))
    monkeypatch.setattr(settings, "_away_blocker", lambda mode=None: None)
    c = _home_client()
    assert c.post("/settings/away", data={"on": "1"}).json() == {"ok": True}
    assert db.get_setting("away_mode") == "1"
    assert c.post("/settings/away", data={"on": "0"}).json() == {"ok": True}
    assert db.get_setting("away_mode") == "0"
    assert calls == [True, True]


@pytest.mark.parametrize("damage", ["short cert", "garbage key", "other pair"])
def test_a_broken_pair_is_mended(damage, caplog):
    first = tls.ensure_own()
    if damage == "short cert":
        first.cert_path.write_bytes(first.cert_path.read_bytes()[:200])
    elif damage == "garbage key":
        first.key_path.write_bytes(b"not a key")
    else:
        other = tmp_dir() / "o"
        other.mkdir()
        import app.tls as t
        t._make(other / "c.pem", other / "k.pem")
        first.key_path.write_bytes((other / "k.pem").read_bytes())
    db.set_setting("away_mode", "1")
    with caplog.at_level("ERROR", logger="couchelephant.tls"):
        t = announce.txt()
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert t["tls"] == "1"
    own = tls.ensure_own()          # now whole, and it matches what was announced
    assert t["fp"] == own.fingerprint
    assert tls.context() is not None
    assert own.key_path.stat().st_mode & 0o077 == 0
    assert not list(own.cert_path.parent.glob("*.tmp"))
    # and the QR still works
    import socket as sk
    announce_addrs = sk.inet_aton("192.168.1.20")
    assert announce_addrs
    assert _home_client().get("/api/connect.svg").status_code == 200


def test_a_non_ascii_host_name_does_not_break_the_certificate(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Owner\u2019s-PC")
    own = tls.ensure_own()
    cert = x509.load_pem_x509_certificate(own.cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    for n in san.get_values_for_type(x509.DNSName):
        n.encode("ascii")
    assert "localhost" in san.get_values_for_type(x509.DNSName)


def test_the_qr_carries_the_fingerprint_when_away_is_on(monkeypatch):
    import urllib.parse

    import segno

    from app.routes import tv
    monkeypatch.setattr(announce, "_addresses",
                        lambda: [socket.inet_aton("192.168.1.20")])
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8123")

    def want(url, fp=None):
        link = "couchelephant://connect?url=" + urllib.parse.quote(url, safe="")
        if fp:
            link += "&fp=" + fp
        return segno.make(link).svg_inline(**tv.QR_LOOK)

    from fastapi.testclient import TestClient

    from app.web import app

    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("192.168.1.5", 4321)}
        await app(scope, receive, send)
    client = TestClient(shim)
    body = lambda r: r.text.replace(' xmlns="http://www.w3.org/2000/svg"', "", 1)  # noqa: E731
    assert body(client.get("/api/connect.svg")) == want("http://192.168.1.20:8123")
    db.set_setting("away_mode", "1")
    fp = tls.ensure_own().fingerprint
    assert body(client.get("/api/connect.svg")) == want("https://192.168.1.20:8123", fp)
