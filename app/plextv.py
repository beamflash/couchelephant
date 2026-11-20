"""plex.tv: the PIN sign-in and the account's server list.

The token this handles is the user's account token. It goes in a header only,
never in a URL, a log line or an error message.
"""
import os
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import paths
from .version import VERSION

BASE = os.environ.get("COUCHELEPHANT_PLEXTV_URL", "https://plex.tv").rstrip("/")
TIMEOUT = 10.0
_UNREADABLE = "plex.tv sent an answer I could not read."


class PlexTvError(Exception):
    """Something went wrong talking to plex.tv. The message suits the page.

    `status` is the HTTP status when plex.tv answered, else None."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class Pin:
    id: int
    code: str
    expires_at: int


@dataclass
class Connection:
    uri: str
    local: bool
    relay: bool
    address: str = ""
    port: int = 0
    protocol: str = ""


@dataclass
class PlexServer:
    name: str
    client_id: str
    access_token: str = field(repr=False)
    connections: list = field(default_factory=list)
    owned: bool = True


def _headers(token=None):
    h = {
        "Accept": "application/json",
        "X-Plex-Product": "CouchElephant",
        "X-Plex-Client-Identifier": paths.install_id(),
        "X-Plex-Version": VERSION,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def _request(method, path, token, params):
    """One request with a total deadline. httpx's timeout is per phase, so a
    slow trickle never trips it; the thread join is the real limit."""
    box = {}

    def run():
        try:
            box["r"] = httpx.request(method, BASE + path, params=params,
                                     headers=_headers(token), timeout=TIMEOUT)
        except BaseException as e:
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(TIMEOUT)
    if t.is_alive():
        raise httpx.ReadTimeout("deadline")
    if "e" in box:
        raise box["e"]
    return box["r"]


def _call(method, path, token=None, params=None, by_status=None):
    try:
        r = _request(method, path, token, params)
    except httpx.TimeoutException:
        raise PlexTvError("plex.tv did not answer.") from None
    except httpx.HTTPError:
        raise PlexTvError("Could not reach plex.tv.") from None
    if r.status_code >= 400:
        msg = (by_status or {}).get(r.status_code)
        raise PlexTvError(msg or f"plex.tv answered with an error ({r.status_code}).",
                          r.status_code)
    try:
        return r.json()
    except ValueError:
        raise PlexTvError(_UNREADABLE) from None


def create_pin() -> Pin:
    d = _call("POST", "/api/v2/pins", params={"strong": "false"})
    try:
        if not isinstance(d, dict):
            raise TypeError
        return Pin(int(d["id"]), str(d["code"]),
                   int(time.time()) + int(d.get("expiresIn") or 900))
    except (KeyError, TypeError, ValueError):
        raise PlexTvError(_UNREADABLE) from None


def check_pin(pin_id: int):
    """The account token once the user has approved the PIN, else None."""
    d = _call("GET", f"/api/v2/pins/{int(pin_id)}",
              by_status={404: "That code has expired. Get a new one."})
    if not isinstance(d, dict):
        raise PlexTvError(_UNREADABLE)
    return d.get("authToken") or None


def _port(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _rank(c: Connection):
    return 0 if c.local and not c.relay else 2 if c.relay else 1


def servers(token: str) -> list:
    d = _call("GET", "/api/v2/resources", token=token,
              params={"includeHttps": "1", "includeRelay": "1"},
              by_status={401: "Plex did not accept the sign-in. Sign in again."})
    if not isinstance(d, list):
        raise PlexTvError(_UNREADABLE)
    out = []
    for r in d:
        if not isinstance(r, dict):
            continue
        if "server" not in (r.get("provides") or "").split(","):
            continue
        conns = [Connection(c.get("uri", ""), bool(c.get("local")),
                            bool(c.get("relay")), str(c.get("address") or ""),
                            _port(c.get("port")), str(c.get("protocol") or ""))
                 for c in r.get("connections") or [] if isinstance(c, dict)]
        conns.sort(key=_rank)
        out.append(PlexServer(r.get("name", ""), r.get("clientIdentifier", ""),
                              r.get("accessToken", ""), conns,
                              bool(r.get("owned"))))
    return out
