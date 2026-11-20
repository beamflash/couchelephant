"""A real certificate for a domain, from Let's Encrypt, on the one port.

Let's Encrypt proves the domain by connecting to it on port 443 over TLS and
asking for the protocol name acme-tls/1 (TLS-ALPN-01, RFC 8737). The server
answers that on the same port it always uses: tls.py hands out a throwaway
challenge certificate for as long as an order runs. The acme library dropped
its own TLS-ALPN-01 support, so the challenge type is defined here.

Files, in <data_dir>/tls/: acme-account.pem, <domain>.cert.pem and
<domain>.key.pem (the key is private, 0600)."""
import datetime
import hashlib
import logging
import os
import re
import threading
import time

import josepy as jose
import requests
from acme import challenges, client, crypto_util, errors, messages
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import db, paths, tls

log = logging.getLogger("couchelephant.acme")

PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30
ORDER_SECONDS = 120
DAY = 86400
# Let's Encrypt allows five failed checks an hour for one name; one try every
# quarter hour stays under that and still lets a fixed router through soon.
RETRY_WAIT = 15 * 60

_lock = threading.Lock()
_wlock = threading.Lock()    # guards _wanted and the hand-back of _lock
_wanted: list = []          # a domain asked for while an order was running
_state = {"domain": None, "working": False}


def directory_url() -> str:
    # The test suite points this at Pebble; nothing else sets it.
    return os.environ.get("COUCHELEPHANT_ACME_DIRECTORY") or PRODUCTION


def _verify():
    return os.environ.get("COUCHELEPHANT_ACME_CA") or True


# ---------- the challenge type ----------

@challenges.ChallengeResponse.register
class TLSALPN01Response(challenges.KeyAuthorizationChallengeResponse):
    typ = "tls-alpn-01"


@challenges.Challenge.register
class TLSALPN01(challenges.KeyAuthorizationChallenge):
    response_cls = TLSALPN01Response
    typ = response_cls.typ

    def validation(self, account_key, **unused):
        return self.key_authorization(account_key)


def challenge_cert(domain: str, key_auth: str):
    """The RFC 8737 certificate: one name, and the SHA-256 of the key
    authorization in the critical acmeIdentifier extension."""
    key = ec.generate_private_key(ec.SECP256R1())
    digest = hashlib.sha256(key_auth.encode()).digest()
    ext = x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.5.5.7.1.31"),
                                     b"\x04\x20" + digest)
    now = datetime.datetime.now(datetime.UTC)
    name = x509.Name([])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]),
                           critical=False)
            .add_extension(ext, critical=True)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


# ---------- the domain ----------

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_HOME_ONLY = (".local", ".home.arpa", ".lan", ".internal")
_INVISIBLE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                        "\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f"
                        "\ufeff\uffa0]")


def clean_domain(text: str) -> str:
    """A host name Let's Encrypt can use, or ValueError with plain words.
    Case, spaces around it and a trailing dot are tidied; anything else that
    would have to change is refused, so what is saved is what was typed."""
    raw = (text or "").strip()
    if _INVISIBLE.search(raw):
        raise ValueError("The name has an invisible character in it. Type it again "
                         "by hand.")
    name = raw.lower().rstrip(".")
    try:
        ascii_name = name.encode("idna").decode("ascii")
        same = ascii_name.encode("ascii").decode("idna") == name
    except UnicodeError:
        raise ValueError("That is not a valid domain name.") from None
    if not same:
        raise ValueError("That name has characters that change when saved. Type it "
                         "in plain letters.")
    name = ascii_name
    for tail in _HOME_ONLY:
        if name.endswith(tail):
            raise ValueError(f"A name ending in {tail} only works inside your home. "
                             "Use a domain you own.")
    labels = name.split(".")
    if len(labels) >= 2 and labels[-1].isdigit():
        raise ValueError("The name must end with a word like .com, not a number.")
    if len(name) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        raise ValueError("That is not a valid domain name. Use something like "
                         "tv.example.com.")
    return name


def domain() -> str:
    return (db.get_setting("away_domain") or "").strip().lower()


# ---------- files and what they hold ----------

def _files(name: str):
    d = tls._dir()
    return d / (name + ".cert.pem"), d / (name + ".key.pem")


_cert_cache: dict = {}


def _stamp(*paths_):
    out = []
    for p in paths_:
        try:
            st = p.stat()
        except OSError:
            return None
        out.append((st.st_mtime_ns, st.st_size))
    return tuple(out)


def real_cert(name: str):
    """The certificate for this name if the files are whole, the key is the
    certificate's, the name is on it and it has not run out; else None. The
    parsed certificate is kept until the files change."""
    if not name:
        return None
    cert_path, key_path = _files(name)
    stamp = _stamp(cert_path, key_path)
    if stamp is None:
        return None
    hit = _cert_cache.get(name)
    if hit and hit[0] == stamp:
        cert = hit[1]
    else:
        try:
            cert = tls._load(cert_path, key_path)
            names = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            if name not in names:
                cert = None
        except Exception:
            cert = None
        _cert_cache[name] = (stamp, cert)
    if cert is None or cert.not_valid_after_utc <= datetime.datetime.now(datetime.UTC):
        return None
    return cert


def valid_until(name: str):
    cert = real_cert(name)
    return cert.not_valid_after_utc if cert else None


def trusted_for(host: str) -> bool:
    """True when this server holds a valid real certificate for the domain
    set in Settings and that is the name asked for."""
    want = domain()
    host = (host or "").strip().lower().rstrip(".")
    return bool(want) and host == want and real_cert(want) is not None


def _account_key():
    path = tls._dir() / "acme-account.pem"
    key = None
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        pass
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        key = ec.generate_private_key(ec.SECP256R1())
        tls._write(path, key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()), 0o600)
    return jose.JWKEC(key=key)


# ---------- what to say ----------

def explain(exc: Exception, name: str) -> str:
    """Plain words for whatever went wrong. The details go to the log."""
    port = paths.port()
    problems = []
    if isinstance(exc, errors.ValidationError):
        for authz in exc.failed_authzrs:
            for c in authz.body.challenges:
                if c.error is not None:
                    problems.append(c.error)
    elif isinstance(exc, messages.Error):
        problems.append(exc)
    elif isinstance(exc, errors.IssuanceError):
        problems.append(exc.error)
    codes = " ".join(str(getattr(p, "typ", "") or "") + " " + str(getattr(p, "code", "") or "")
                     for p in problems)
    if "rateLimited" in codes:
        return ("Let's Encrypt says too many certificates were asked for this name "
                "lately. Wait an hour and try again.")
    if "dns" in codes:
        return (f"The name {name} does not point to this server. Set its DNS record "
                "to your home's internet address, then try again.")
    if "connection" in codes or "tls" in codes or "unauthorized" in codes:
        return ("Let's Encrypt could not reach this server on port 443; forward "
                f"port 443 on your router to port {port} on this computer.")
    if isinstance(exc, (errors.TimeoutError, requests.Timeout, TimeoutError)):
        return "Let's Encrypt took too long to answer. Try again in a few minutes."
    if isinstance(exc, (requests.ConnectionError, OSError)):
        return ("This computer could not reach Let's Encrypt. Check its internet "
                "connection, then try again.")
    return "Let's Encrypt could not give a certificate. Try again in a few minutes."


def status() -> dict:
    """{domain, state: none|working|ok|error|waiting, say, until}."""
    name = domain()
    if not name:
        return {"domain": "", "state": "none", "say": "", "until": None}
    until = valid_until(name)
    err = db.get_setting("away_domain_error") or ""
    if _lock.locked():
        return {"domain": name, "state": "working",
                "say": "Getting a certificate...", "until": None}
    if until:
        return {"domain": name, "state": "ok", "until": until.date().isoformat(),
                "say": f"Certificate valid until {until.date().isoformat()}"}
    if err:
        return {"domain": name, "state": "error", "say": err, "until": None}
    return {"domain": name, "state": "waiting", "until": None,
            "say": "No certificate yet. Press Try again."}


def _set_error(text: str) -> None:
    db.set_setting("away_domain_error", text)


# ---------- the order ----------

def too_soon(name: str) -> int:
    """Seconds still to wait before this name may be tried again: one attempt
    every 15 minutes, except for a name that was not the last one tried. The time is in
    the database, so a restart does not order again at once."""
    if (db.get_setting("away_domain_last_name") or "") != name:
        return 0
    try:
        last = int(db.get_setting("away_domain_last_try") or 0)
    except ValueError:
        return 0
    return max(0, last + RETRY_WAIT - int(time.time()))


def request(name: str) -> None:
    """Get a certificate for this name. One order runs at a time: a second
    call while one runs returns at once, and if it names a different domain
    that one is ordered when the first is done. Nothing is ordered while the
    certificate is good, or within 15 minutes of the last try for the same name."""
    with _wlock:
        if not _lock.acquire(blocking=False):
            _wanted[:] = [name]
            return
    while True:
        try:
            _state["domain"] = name
            if not _skip(name):
                db.set_setting("away_domain_last_name", name)
                db.set_setting("away_domain_last_try", int(time.time()))
                _run(name)
        except BaseException:
            _release()
            raise
        with _wlock:                 # the queue is read and the lock let go as one step
            nxt = _wanted.pop() if _wanted else None
            if not nxt or nxt == name:
                _lock.release()
                return
        name = nxt


def _release():
    with _wlock:
        _wanted.clear()
        _lock.release()


def _skip(name: str) -> bool:
    until = valid_until(name)
    if until and until - datetime.datetime.now(datetime.UTC) >= \
            datetime.timedelta(days=RENEW_DAYS):
        return True
    return too_soon(name) > 0


def _run(name: str) -> None:
    from . import exposure
    try:
        if not exposure.away_on():
            raise _Say("Turn on Use away from home first; the certificate "
                       "is asked for on the same port.")
        _order(name)
        _set_error("")
        log.info("a real certificate for %s is in place", name)
    except _Say as e:
        _set_error(str(e))
    except Exception as e:
        log.warning("certificate order for %s failed: %s: %s", name,
                    type(e).__name__, e, exc_info=log.isEnabledFor(logging.DEBUG))
        _set_error(explain(e, name))


class _Say(Exception):
    pass


def _order(name: str) -> None:
    jwk = _account_key()
    net = client.ClientNetwork(jwk, alg=jose.ES256, verify_ssl=_verify(),
                               user_agent="CouchElephant", timeout=30)
    acme = client.ClientV2(client.ClientV2.get_directory(directory_url(), net), net)
    try:
        acme.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
    except errors.ConflictError as e:
        net.account = acme.query_registration(
            messages.RegistrationResource(uri=e.location, body=messages.Registration()))
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    orderr = acme.new_order(crypto_util.make_csr(key_pem, [name]))
    try:
        for authz in orderr.authorizations:
            challb = next((c for c in authz.body.challenges
                           if isinstance(c.chall, TLSALPN01)), None)
            if challb is None:
                raise _Say("Let's Encrypt did not offer a way to check this name here.")
            cert_pem, ckey_pem = challenge_cert(
                name, challb.chall.key_authorization(jwk))
            tls.challenge_set(name, cert_pem, ckey_pem)
            acme.answer_challenge(challb, challb.chall.response(jwk))
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=ORDER_SECONDS)
        orderr = acme.poll_and_finalize(orderr, deadline)
    finally:
        tls.challenge_clear()
    cert_path, key_path = _files(name)
    tls._write(key_path, key_pem, 0o600)
    tls._write(cert_path, orderr.fullchain_pem.encode(), 0o644)


# ---------- renewal ----------

def renew_if_due() -> None:
    """Run once a day: order again when the certificate has 30 days left,
    or has run out, or was never made."""
    from . import exposure
    name = domain()
    if not name or not exposure.away_on() or _lock.locked():
        return
    until = valid_until(name)
    now = datetime.datetime.now(datetime.UTC)
    if until is None or until - now < datetime.timedelta(days=RENEW_DAYS):
        request(name)


_last_renew = [0.0]


def renew_daily() -> None:
    """Called from a loop that ticks often; does the work once a day."""
    if time.monotonic() - _last_renew[0] < DAY and _last_renew[0]:
        return
    _last_renew[0] = time.monotonic()
    renew_if_due()


def drop_error() -> None:
    _set_error("")


def start_in_background(name: str) -> None:
    threading.Thread(target=request, args=(name,), daemon=True,
                     name="acme-order").start()

