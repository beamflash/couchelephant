"""Scrubbing for problem reports.

Every report passes through here before it is sealed. Two rules: only settings
named on purpose are ever read, and every string loses anything that looks like
a secret. Addresses are masked so a report never names the owner's network.
"""
import hashlib
import ipaddress
import os
import re
from urllib.parse import urlsplit

REMOVED = "[removed]"

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
NEVER_SENT = ("plex_token", "tmdb_key", "sportsdb_key", "tuner_detected")

_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "127.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]


def _is_private(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in _PRIVATE_NETS if n.version == ip.version)


_KEYWORDS = r"(?:password|passwd|secret|api[_-]?key|token|key|auth)"
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----",
                  re.S)
_AUTH_HEADER = re.compile(
    r"Authorization\s*:\s*(?:(?:Bearer|Basic|Token)\s+)?\S+", re.I)
_BEARER = re.compile(r"\bBearer\s+\S+", re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
# Private halves of an EC/RSA JWK, which may have no digits to trip the
# long-run rule below.
_JWK_PRIVATE = re.compile(r'"(?:d|p|q|dp|dq|qi)"\s*:\s*"[^"]+"')
_KV = re.compile(
    r"(?<![A-Za-z])" + _KEYWORDS + r"""["']?\s*[=:]\s*["']?[^\s"',;&]+""",
    re.I)
_HEX64 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://[^\s/\"'<>?#]+)")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])")


def _mixed_run(m: re.Match) -> str:
    s = m.group(0)
    if any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
        return REMOVED
    return s


class Scrubber:
    def __init__(self, salt: bytes | None = None):
        self.salt = salt if salt is not None else os.urandom(16)

    def host(self, name: str) -> str:
        digest = hashlib.sha256(self.salt + name.encode()).hexdigest()
        return "host-" + digest[:10]

    def address(self, url_or_hostport: str) -> str:
        raw = url_or_hostport.strip()
        has_scheme = "://" in raw
        parts = urlsplit(raw if has_scheme else "//" + raw)
        # hostname drops the user:pass@ part and the brackets of an IPv6.
        name = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        shown = "private-lan" if _is_private(name) else self.host(name)
        out = shown + (f":{port}" if port else "")
        return f"{parts.scheme}://{out}" if has_scheme else out

    def _mask_url(self, m: re.Match) -> str:
        return self.address(m.group(1))

    def _mask_ip(self, m: re.Match) -> str:
        s = m.group(0)
        host = s.split(":")[0]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return s
        return self.address(s)

    def text(self, s: str) -> str:
        if not isinstance(s, str):
            return s
        s = _URL.sub(self._mask_url, s)
        s = _IPV4.sub(self._mask_ip, s)
        s = _PEM.sub(REMOVED, s)
        s = _AUTH_HEADER.sub(REMOVED, s)
        s = _BEARER.sub(REMOVED, s)
        s = _JWT.sub(REMOVED, s)
        s = _JWK_PRIVATE.sub(REMOVED, s)
        s = _KV.sub(REMOVED, s)
        s = _HEX64.sub(REMOVED, s)
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
