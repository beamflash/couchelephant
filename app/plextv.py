"""plex.tv: the PIN sign-in and the account's server list.

The token this handles is the user's account token. It goes in a header only,
never in a URL, a log line or an error message.
"""
import os
import time
from dataclasses import dataclass, field

import httpx

from . import paths
from .version import VERSION

BASE = os.environ.get("COUCHELEPHANT_PLEXTV_URL", "https://plex.tv").rstrip("/")
TIMEOUT = 10.0


class PlexTvError(Exception):
    """Something went wrong talking to plex.tv. The message suits the page."""


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


@dataclass
class PlexServer:
    name: str
    client_id: str
    access_token: str
    connections: list = field(default_factory=list)


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


def _call(method, path, token=None, params=None):
    try:
        r = httpx.request(method, BASE + path, params=params,
                          headers=_headers(token), timeout=TIMEOUT)
    except httpx.TimeoutException:
        raise PlexTvError("plex.tv did not answer.") from None
    except httpx.HTTPError:
        raise PlexTvError("Could not reach plex.tv.") from None
    if r.status_code >= 400:
        raise PlexTvError(f"plex.tv answered with an error ({r.status_code}).")
    try:
        return r.json()
    except ValueError:
        raise PlexTvError("plex.tv sent an answer I could not read.") from None


def create_pin() -> Pin:
    d = _call("POST", "/api/v2/pins", params={"strong": "false"})
    try:
        return Pin(int(d["id"]), str(d["code"]),
                   int(time.time()) + int(d.get("expiresIn") or 900))
    except (KeyError, TypeError, ValueError):
        raise PlexTvError("plex.tv sent an answer I could not read.") from None


def check_pin(pin_id: int):
    """The account token once the user has approved the PIN, else None."""
    d = _call("GET", f"/api/v2/pins/{int(pin_id)}")
    return d.get("authToken") or None


def _rank(c: Connection):
    return 0 if c.local and not c.relay else 2 if c.relay else 1


def servers(token: str) -> list:
    d = _call("GET", "/api/v2/resources", token=token,
              params={"includeHttps": "1", "includeRelay": "1"})
    out = []
    for r in d if isinstance(d, list) else []:
        if "server" not in (r.get("provides") or "").split(","):
            continue
        conns = [Connection(c.get("uri", ""), bool(c.get("local")),
                            bool(c.get("relay")))
                 for c in r.get("connections") or []]
        conns.sort(key=_rank)
        out.append(PlexServer(r.get("name", ""), r.get("clientIdentifier", ""),
                              r.get("accessToken", ""), conns))
    return out
