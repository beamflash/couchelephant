"""Scrubbing for problem reports.

Every report passes through here before it is sealed. Two rules: only settings
named on purpose are ever read, and every string loses anything that looks like
a secret. Addresses are masked so a report never names the owner's network.
"""
import hashlib
import ipaddress
import os
import re
from collections.abc import Iterable

from . import backingstore, dbstore

REMOVED = "[removed]"

# Ported to Swift: when a pattern changes, update the shared scrub_corpus.json and rerun both suites.

# A new setting is invisible to reports until it is added here on purpose.
SETTINGS_ALLOW = (
    "timezone", "sync_minutes", "dry_run", "epg_provider",
    "sports_section", "shows_section", "movies_section",
    "recorder_default", "recorder_encoder", "recorder_lead_minutes",
    "live_pause_minutes", "adskip_live", "adskip_final", "sports_overtime",
    "overtime_cap_minutes", "recorder_trail_minutes",
    "recorder_shows_folder", "recorder_shows_section",
    "recorder_movies_folder", "recorder_movies_section", "recorder_keep_days",
)
# Sent, but only after the address is masked.
ADDRESS_SETTINGS = ("plex_url", "tuner_url")
# Known settings that are never sent. Listed so db.DEFAULTS can't grow a
# setting without someone deciding which list it belongs in.
NEVER_SENT = ("plex_token", "tmdb_key", "sportsdb_key", "tuner_detected",
              "report_consent")
# Every setting any part of the app treats as a secret, from one place: ours,
# the export code's and the backing stores' (their passwords are stored raw
# and can be any shape, so a caller must hand their values to the Scrubber).
SECRET_SETTINGS_ALL = tuple(sorted(
    set(NEVER_SENT) | set(dbstore.SECRET_SETTINGS) | set(backingstore.SECRET_KEYS)))
# Settings that name a machine; their host part is masked wherever it appears.
HOST_SETTINGS = ("plex_url", "tuner_url", "tuner_detected", "pg_host", "my_host",
                 "away_domain", "cf_team_domain")

_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "127.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]


def _is_private(host: str) -> bool:
    if _lc(host) == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in _PRIVATE_NETS if n.version == ip.version)


# Word characters and whitespace are spelled out, never \w \s \b: Python and
# ICU (Swift) disagree on combining marks, joiners, \v and a few controls, and
# a secret next to one must not slip past either engine. Same text in Swift.
_WS = r" \x09-\x0d\x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_S = "[" + _WS + "]"
_NS = "[^" + _WS + "]"


def _ci(lit: str) -> str:
    """A literal that ignores ASCII case, spelled out. re.I would also let
    U+212A, U+017F and friends match, and ICU (Swift) does not agree with it."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isascii() and c.isalpha()
                   else re.escape(c) for c in lit)


def _ci_any(*words: str) -> str:
    return "(?:" + "|".join(_ci(w) for w in words) + ")"


_UP = {c: c + 32 for c in range(65, 91)}
_SPACE = re.compile(_S)


def cut_take(s: str, n: int) -> str:
    """The first n characters of a line to be scrubbed later. A line that is
    cut also loses the word the cut fell in (back to the last whitespace, or
    all of it when there is none), so no half of a secret is left for the
    scrub to miss. Same in Swift."""
    if len(s) <= n:
        return s
    i = n
    while i > 0 and not _SPACE.match(s[i - 1]):
        i -= 1
    return s[:i]


def _lc(s: str) -> str:
    """Lower case for ASCII letters only, the same in every language. A
    non-ASCII host name therefore matches only in the case it was given: an
    accepted trade-off, because ICU and Python fold such letters differently."""
    return s.translate(_UP)


# The keyword, lower case after a non-letter or camelCase after a lower letter
# (accessToken, clientSecret, apiKey).
_KEY = ("(?:(?<![A-Za-z])(?:" + "|".join(
    [_ci("password"), _ci("passwd"), _ci("secret"), _ci("api") + "[_-]?" + _ci("key"),
     _ci("token"), _ci("key"), _ci("auth")])
    + ")|(?<=[a-z])(?:Password|Passwd|Secret|ApiKey|Token|Key|Auth))")
_AUTH_HEADER = re.compile(
    _ci("Authorization") + _S + "*:" + _S + "*(?:" + _ci_any("Bearer", "Basic", "Token")
    + _S + "+)?" + _NS + "+")
_COOKIE = re.compile("(?:" + _ci("Set-") + ")?" + _ci("Cookie") + _S + r"*:[^\n]*")
_BEARER = re.compile("(?<![A-Za-z0-9_])" + _ci("Bearer") + _S + "+" + _NS + "+")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
# Private halves of an EC/RSA JWK, which may have no digits to trip the
# long-run rule below.
_JWK_PRIVATE = re.compile(r'"(?:d|p|q|dp|dq|qi)"' + _S + "*:" + _S + r'*"[^"]+"')
# keyword = value. A quoted value goes to its closing quote, an unquoted one
# to the next space, or to & so a query string keeps its other parameters.
_KV = re.compile(
    _KEY + """["']?""" + _S + "*[=:]" + _S + """*(?:"[^"\\n]*"|'[^'\\n]*'|[^""" + _WS + "&]+)")
# "token Zx_cVbNm..." with nothing between the word and the secret.
_KW_SPACE = re.compile(
    "(?<![A-Za-z])" + _ci_any("token", "key", "secret", "password")
    + r"[ \t]+[A-Za-z0-9_-]{16,}")
_HEX32 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
# Bounded so a long dotted string can't make the scan quadratic. The body is
# the whole run up to a space or quote; _mask_url finds the host inside it.
_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]{0,30}://"
# The body stops where another scheme:// starts, so a URL nested in a path or
# query is a match of its own and is masked on its own.
_URL = re.compile(_SCHEME + "((?:(?!" + _SCHEME + ")[^" + _WS + "\"'<>]){1,1000})")
# The host after an @ even when the user:pass in front of it was too long for
# the URL pass to reach (also masks the host half of an email address).
_AT_HOST = re.compile(
    r"(?<=@)(?:[A-Za-z0-9-]{1,63}\.){1,6}[A-Za-z]{2,24}(?::([0-9]{2,5}))?(?![A-Za-z0-9_-])")
_IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]{1,5})?(?![0-9.])")
_V6_BRACKET = re.compile(
    r"\[([0-9A-Fa-f:.]{2,45})(?:%[A-Za-z0-9_.-]{1,20})?\](?::([0-9]{1,5}))?")
_V6_BARE = re.compile(
    r"(?<![A-Za-z0-9_:.])([0-9A-Fa-f:]{2,39})(?:%[A-Za-z0-9_.-]{1,20})?(?![A-Za-z0-9_:])")
# Bare text only counts as IPv6 when it looks like an address a LAN or a
# provider hands out, so "dead::beef" and "a::b" are left alone.
_V6_SHAPE = re.compile(
    r"::1|fe[89ab][0-9a-f]:.*|f[cd][0-9a-f]{0,2}:.*|[23][0-9a-f]{3}:.*"
    r"|(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}", re.I)
_LOCAL_NAME = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9-]{1,63}\.){1,6}"
    + _ci_any("lan", "local", "localdomain", "home", "internal", "arpa")
    + r"(?::([0-9]{1,5}))?(?![A-Za-z0-9_-])")
# name:port in free text. A dotted name is a host unless it ends like a source
# file ("app.py:1234") or a number ("1.5.33:12"). A single word is a host only
# after a connection word or an @, or when it is a known host: "Quality:1080p",
# "season:2024" and "request:1102" are ordinary text.
_CODE_EXT = {"py", "js", "ts", "swift", "go", "rs", "c", "h", "cpp", "java",
             "kt", "rb", "sh", "html", "json", "yaml", "yml", "toml", "txt",
             "log", "md"}
_DOTTED_PORT = re.compile(
    r"(?<![A-Za-z0-9_.:/@-])([A-Za-z][A-Za-z0-9-]{0,62}(?:\.[A-Za-z0-9-]{1,63}){1,6})"
    r":([0-9]{2,5})(?![A-Za-z0-9_:.-])")
_WORD_PORT = re.compile(
    "((?<![A-Za-z0-9_-])" + _ci_any("host", "connect", "connecting", "connection", "to",
                                    "from", "server", "address", "addr", "upstream",
                                    "proxy", "peer")
    + r"[ \t]{1,3}|@)([A-Za-z][A-Za-z0-9-]{0,62}):([0-9]{2,5})(?![A-Za-z0-9_:.-])")
_ALREADY = re.compile(r"private-lan|host-[0-9a-f]{10}")


def _cut_pem(s: str) -> str:
    """Remove each PEM block. One with no END line goes to the end of the text."""
    begin = re.compile(r"-----BEGIN [A-Z0-9 ]{1,40}-----")
    out, pos = [], 0
    while True:
        m = begin.search(s, pos)
        if not m:
            out.append(s[pos:])
            break
        out.append(s[pos:m.start()])
        out.append(REMOVED)
        e = re.compile(r"-----END [A-Z0-9 ]{1,40}-----").search(s, m.end())
        if not e:
            break
        pos = e.end()
    return "".join(out)


def _split_hostport(raw: str) -> tuple[str, int | None]:
    """Host and port of a URL or host:port, minus any user:pass@. Raises
    ValueError on a shape it can't read."""
    rest = raw.strip()
    if "://" in rest:
        rest = rest.split("://", 1)[1]
    # Up to the LAST @, so a password holding / ? # or @ leaves no tail.
    rest = rest.rpartition("@")[2]
    rest = re.split(r"[/?#]", rest, maxsplit=1)[0]
    port = None
    if rest.startswith("["):
        host, sep, tail = rest[1:].partition("]")
        if not sep or (tail and not tail.startswith(":")):
            raise ValueError("bad bracketed host")
        rest_port = tail[1:]
    elif rest.count(":") == 1:
        host, _, rest_port = rest.partition(":")
    else:
        host, rest_port = rest, ""
    if rest_port:
        if not rest_port.isdigit() or int(rest_port) > 65535:
            raise ValueError("bad port")
        port = int(rest_port)
    host = _lc(host.split("%", 1)[0].strip())
    if not host:
        raise ValueError("no host")
    return host, port


def _mixed_run(m: re.Match) -> str:
    s = m.group(0)
    if any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
        return REMOVED
    return s


class Scrubber:
    def __init__(self, salt: bytes | None = None,
                 known_secrets: Iterable[str] = (),
                 known_hosts: Iterable[str] = ()):
        self.salt = salt if salt is not None else os.urandom(16)
        self._shown_cache: dict[str, str] = {}
        self._host_cache: dict[str, str] = {}
        # Longest first, so a secret that holds another is removed whole.
        # Ties by value, so the order never depends on a set's.
        self._secrets = sorted({v for v in known_secrets
                                if isinstance(v, str) and len(v) >= 4},
                               key=lambda v: (-len(v), v))
        names = set()
        for h in known_hosts:
            try:
                names.add(_split_hostport(h)[0])
            except (ValueError, AttributeError):
                continue
        self._hosts = sorted(names, key=lambda n: (-len(n), n))

    def host(self, name: str) -> str:
        got = self._host_cache.get(name)
        if got is None:
            digest = hashlib.sha256(
                self.salt + name.encode(errors="surrogatepass")).hexdigest()
            got = self._host_cache[name] = "host-" + digest[:10]
        return got

    def _shown(self, name: str) -> str:
        name = _lc(name)
        got = self._shown_cache.get(name)
        if got is None:
            got = "private-lan" if _is_private(name) else self.host(name)
            self._shown_cache[name] = got
        return got

    def address(self, url_or_hostport: str) -> str:
        raw = url_or_hostport.strip()
        try:
            name, port = _split_hostport(raw)
        except ValueError:
            # Can't read it, so say nothing about it.
            return self.host(raw)
        out = self._shown(name) + (f":{port}" if port else "")
        if "://" in raw:
            return f"{raw.split('://', 1)[0]}://{out}"
        return out

    def _mask_url(self, m: re.Match) -> str:
        scheme = m.group(0).split("://", 1)[0]
        body = m.group(1)
        # Everything up to the LAST @ is user:pass, whatever it holds.
        _, at, rest = body.rpartition("@")
        cut = re.search(r"[/?#]", rest)
        host, tail = (rest[:cut.start()], rest[cut.start():]) if cut else (rest, "")
        return self.address(f"{scheme}://{host}") + tail

    def _mask_at_host(self, m: re.Match) -> str:
        port = m.group(1)
        name = m.group(0).rsplit(":", 1)[0] if port else m.group(0)
        return self._shown(name) + (f":{port}" if port else "")

    def _mask_ip(self, m: re.Match) -> str:
        s = m.group(0)
        try:
            ipaddress.ip_address(s.split(":")[0])
        except ValueError:
            return s
        return self.address(s)

    def _mask_v6_bracket(self, m: re.Match) -> str:
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            return m.group(0)
        out = self._shown(m.group(1))
        return out + (f":{m.group(2)}" if m.group(2) else "")

    def _mask_v6_bare(self, m: re.Match) -> str:
        s = m.group(1)
        if s.count(":") < 2:
            return m.group(0)
        if not _V6_SHAPE.fullmatch(s):
            return m.group(0)
        try:
            ipaddress.IPv6Address(s)
        except ValueError:
            return m.group(0)
        return self._shown(s)

    def _mask_local(self, m: re.Match) -> str:
        s = m.group(0)
        name = s.rsplit(":", 1)[0] if m.group(1) else s
        return self.host(_lc(name)) + (f":{m.group(1)}" if m.group(1) else "")

    def _mask_dotted_port(self, m: re.Match) -> str:
        last = _lc(m.group(1).rsplit(".", 1)[1])
        if last in _CODE_EXT or last.isdigit():
            return m.group(0)
        return self.host(_lc(m.group(1))) + ":" + m.group(2)

    def _mask_word_port(self, m: re.Match) -> str:
        if _ALREADY.fullmatch(m.group(2)):
            return m.group(0)
        return m.group(1) + self.host(_lc(m.group(2))) + ":" + m.group(3)

    def text(self, s: str) -> str:
        if not isinstance(s, str):
            return s
        for v in self._secrets:
            s = s.replace(v, REMOVED)
        s = _cut_pem(s)
        s = _URL.sub(self._mask_url, s)
        s = _AT_HOST.sub(self._mask_at_host, s)
        s = _V6_BRACKET.sub(self._mask_v6_bracket, s)
        s = _IPV4.sub(self._mask_ip, s)
        s = _V6_BARE.sub(self._mask_v6_bare, s)
        s = _LOCAL_NAME.sub(self._mask_local, s)
        s = _DOTTED_PORT.sub(self._mask_dotted_port, s)
        s = _WORD_PORT.sub(self._mask_word_port, s)
        for h in self._hosts:
            s = re.sub(r"(?<![A-Za-z0-9_.-])" + _ci(h) + r"(?![A-Za-z0-9_-])",
                       self.host(h), s)
        s = _AUTH_HEADER.sub(REMOVED, s)
        s = _COOKIE.sub(REMOVED, s)
        s = _BEARER.sub(REMOVED, s)
        s = _JWT.sub(REMOVED, s)
        s = _JWK_PRIVATE.sub(REMOVED, s)
        s = _KV.sub(REMOVED, s)
        s = _KW_SPACE.sub(REMOVED, s)
        s = _HEX32.sub(REMOVED, s)
        return _RUN.sub(_mixed_run, s)

    def settings(self, all_settings: dict[str, str]) -> dict[str, str]:
        out = {}
        for k in SETTINGS_ALLOW:
            if k in all_settings:
                out[k] = self.text(all_settings[k])
        for k in ADDRESS_SETTINGS:
            if k in all_settings:
                v = all_settings[k]
                out[k] = self.address(v) if v else v
        return out
