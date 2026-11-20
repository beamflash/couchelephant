"""The server's own certificate, for Use away from home.

ECDSA P-256, ten years, made once in <data_dir>/tls/. The apps pin the public
key, not the certificate, so the fingerprint is the SHA-256 of the key's
SubjectPublicKeyInfo, base64url without padding. A reinstall that lost the
data folder makes a new key; the apps learn it the next time they are home."""
import base64
import datetime
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
import threading
from pathlib import Path
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import paths

log = logging.getLogger("couchelephant.tls")

YEARS = 10
_lock = threading.Lock()
_ctx_cache: dict = {}


class OwnCert(NamedTuple):
    cert_path: Path
    key_path: Path
    fingerprint: str


def _dir() -> Path:
    d = paths.data_dir() / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fingerprint_of(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER,
                                  serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def _names() -> list:
    """The host names and LAN addresses the certificate answers to."""
    from . import announce
    host = socket.gethostname().split(".")[0] or "server"
    out = []
    for n in dict.fromkeys([host, host + ".local", announce.host_label() + ".local",
                            "localhost"]):
        try:
            out.append(x509.DNSName(n.encode("idna").decode("ascii")))
        except (UnicodeError, ValueError):
            log.info("left %r out of the certificate: not a valid host name", n)
    ips = ["127.0.0.1", "::1"]
    try:
        ips += [socket.inet_ntoa(a) for a in announce._addresses()]
    except Exception as e:
        log.debug("no LAN addresses for the certificate: %s", e)
    out += [x509.IPAddress(ipaddress.ip_address(i)) for i in dict.fromkeys(ips)]
    return out


def _make(cert_path: Path, key_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CouchElephant")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365 * YEARS))
            .add_extension(x509.SubjectAlternativeName(_names()), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    # Each file is written whole under a temp name, then moved into place;
    # the key is private from its first byte. A crash leaves the old pair or
    # a mismatch that ensure_own() mends.
    _write(key_path, pem_key, 0o600)
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load(cert_path: Path, key_path: Path):
    """The certificate if both files load and the key is the cert's."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if key.public_key().public_bytes(*fmt) != cert.public_key().public_bytes(*fmt):
        raise ValueError("the key is not the certificate's")
    return cert


def ensure_own() -> OwnCert:
    """Make the certificate if it is not there, then say where it is."""
    with _lock:
        d = _dir()
        cert_path, key_path = d / "cert.pem", d / "key.pem"
        cert = None
        if cert_path.is_file() and key_path.is_file():
            try:
                cert = _load(cert_path, key_path)
            except Exception as e:
                log.error("the own certificate is unusable (%s); making a new one. "
                          "Apps will learn the new fingerprint at home.", e)
        if cert is None:
            _make(cert_path, key_path)
            cert = _load(cert_path, key_path)
        return OwnCert(cert_path, key_path, fingerprint_of(cert.public_key()))


def _stamp(own_dir: Path, domain: str = ""):
    out = []
    names = ["cert.pem", "key.pem"]
    if domain:
        names += [domain + ".cert.pem", domain + ".key.pem"]
    for n in names:
        try:
            st = (own_dir / n).stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            if n in ("cert.pem", "key.pem"):
                return None
            out.append(None)            # no real certificate yet
    return tuple(out)


# The throwaway certificate an ACME order answers with: {domain: SSLContext}.
_challenge: dict = {}


def challenge_set(domain: str, cert_pem: bytes, key_pem: bytes) -> None:
    """Answer acme-tls/1 for this name until challenge_clear()."""
    d = _dir()
    cert_path, key_path = d / "challenge.cert.pem", d / "challenge.key.pem"
    _write(key_path, key_pem, 0o600)
    _write(cert_path, cert_pem, 0o644)
    try:
        ctx = _new_context()
        ctx.load_cert_chain(cert_path, key_path)
        ctx.set_alpn_protocols(["acme-tls/1"])
    finally:
        cert_path.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
    _challenge[domain.lower()] = ctx


def challenge_clear() -> None:
    _challenge.clear()


def _new_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    return ctx


def _real_context(domain: str):
    """A context for the real certificate, or None when there is not a good one."""
    from . import acme_tls
    if not domain or acme_tls.real_cert(domain) is None:
        return None
    cert_path, key_path = acme_tls._files(domain)
    ctx = _new_context()
    try:
        ctx.load_cert_chain(cert_path, key_path)
    except (ssl.SSLError, OSError) as e:
        log.warning("the real certificate for %s did not load: %s", domain, e)
        return None
    return ctx


def _chooser(domain: str, real):
    """The SNI callback: the throwaway certificate while an order runs for
    that name, the real certificate for that name, the own one for anything
    else (including no name at all)."""
    def pick(sslobj, name, _ctx):
        name = (name or "").lower().rstrip(".")
        if not name or name != domain:
            return None
        try:
            chall = _challenge.get(name)
            if chall is not None:
                sslobj.context = chall
            elif real is not None:
                sslobj.context = real
        except Exception as e:
            log.warning("could not pick a certificate for %s: %s", name, e)
            return ssl.ALERT_DESCRIPTION_INTERNAL_ERROR
        return None
    return pick


def context():
    """The SSLContext to serve with, or None while away is off (then the
    port speaks plain HTTP only, as it always did). Built once and kept until
    the certificate files change; the setting is read every time. It serves
    the own certificate, and the real one for the domain in Settings."""
    from . import exposure
    if not exposure.away_on():
        return None
    from . import acme_tls
    domain = acme_tls.domain()
    d = _dir()
    stamp = _stamp(d, domain)
    hit = _ctx_cache.get(d)
    if stamp is not None and hit and hit[0] == stamp and hit[2] == domain:
        return hit[1]
    own = ensure_own()
    ctx = _new_context()
    ctx.load_cert_chain(own.cert_path, own.key_path)
    if domain:
        ctx.sni_callback = _chooser(domain, _real_context(domain))
    _ctx_cache.clear()
    _ctx_cache[d] = (_stamp(d, domain), ctx, domain)
    return ctx


def fingerprint_if_away():
    """The fingerprint to announce, or None when away is off."""
    from . import exposure
    return ensure_own().fingerprint if exposure.away_on() else None
