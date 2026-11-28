"""The HDHomeRun boxes CouchElephant knows: the rows, and what each one says.

`tuner.py` is what the rest of the app calls. This file owns the table and the
cached answers from each box.
"""
import hashlib
import threading
import time
from urllib.parse import urlsplit

import httpx

from . import db

PROBE_TIMEOUT = 2
LINEUP_TTL = 600            # a lineup is trusted this long
LINEUP_KEEP = 3600          # and a failed read keeps the last good one this long
STATUS_TTL = 5              # one tick asks each box at most once
STATUS_FAIL_TTL = 15        # a box that did not answer is left alone a while

_lock = threading.Lock()
_lineups: dict = {}
_statuses: dict = {}


def reset_cache() -> None:
    with _lock:
        _lineups.clear()
        _statuses.clear()


def normalise(address: str) -> str:
    """`http://host[:port]`: scheme added, path and trailing slash gone."""
    a = (address or "").strip()
    if not a:
        return ""
    if "://" not in a:
        a = "http://" + a
    p = urlsplit(a)
    host = (p.netloc or "").lower()
    return f"{p.scheme.lower()}://{host}" if host else ""


def _probe(address: str) -> tuple[dict | None, str | None]:
    """Ask a box who it is. Its /discover.json body, or why it did not say."""
    try:
        r = httpx.get(address + "/discover.json", timeout=PROBE_TIMEOUT)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError("not an HDHomeRun answer")
        return body, None
    except (httpx.HTTPError, ValueError) as e:
        return None, f"The tuner at {address} did not answer: {e}"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fields(info: dict) -> dict:
    return {"device_id": (info.get("DeviceID") or "").strip() or None,
            "name": info.get("FriendlyName"), "model": info.get("ModelNumber"),
            "firmware": info.get("FirmwareVersion"),
            "tuner_count": _int(info.get("TunerCount"))}


def _rows(enabled_only=False):
    where = "WHERE enabled = 1 " if enabled_only else ""
    return db.query(f"SELECT * FROM tuner_boxes {where}"
                    "ORDER BY found_by = 'typed' DESC, id")


def rows(enabled_only: bool = False):
    """Every box, typed ones first, then by id."""
    migrate_settings()
    return _rows(enabled_only)


def get(box_id: int):
    return db.one("SELECT * FROM tuner_boxes WHERE id = ?", (box_id,))


def _same_box(device_id, count):
    """A row the same physical box as this DeviceID and tuner count, if any."""
    if not device_id:
        return None
    return db.one("SELECT * FROM tuner_boxes WHERE device_id = ? AND tuner_count IS ? "
                  "ORDER BY found_by = 'typed' DESC, id", (device_id, count))


def _refresh(box_id: int, info: dict) -> None:
    f = _fields(info)
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET device_id = ?, name = ?, model = ?, firmware = ?, "
                  "tuner_count = ?, last_seen = ?, error = NULL WHERE id = ?",
                  (f["device_id"], f["name"], f["model"], f["firmware"], f["tuner_count"],
                   int(time.time()), box_id))


def add_typed(address: str) -> int:
    """A box the person named. Asked once who it is; kept even if it is silent."""
    addr = normalise(address)
    if not addr:
        raise ValueError("That is not an address.")
    have = db.one("SELECT id FROM tuner_boxes WHERE address = ? AND found_by = 'typed'", (addr,))
    if have:
        return have["id"]
    info, err = _probe(addr)
    f = _fields(info) if info else {}
    same = _same_box(f.get("device_id"), f.get("tuner_count")) if info else None
    if same:
        # Found already (by Plex or the network): the typed address replaces
        # the found one, and the row keeps its place.
        with db.tx() as c:
            c.execute("UPDATE tuner_boxes SET address = ?, found_by = 'typed' WHERE id = ?",
                      (addr, same["id"]))
        _refresh(same["id"], info)
        return same["id"]
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute("INSERT INTO tuner_boxes (address, found_by, enabled, added_at, error) "
                        "VALUES (?, 'typed', 1, ?, ?)", (addr, now, err))
        box_id = cur.lastrowid
    if info:
        _refresh(box_id, info)
    return box_id


def add_found(address: str, found_by: str, info: dict) -> int | None:
    """A box Plex or the network named. `info` is its /discover.json body.

    The same box is one row: by address, or by DeviceID with the same tuner
    count. A typed row keeps its address; a found row keeps the address it was
    first found at.
    """
    addr = normalise(address)
    if not addr:
        return None
    f = _fields(info or {})
    have = db.one("SELECT * FROM tuner_boxes WHERE address = ? ORDER BY found_by = 'typed' DESC",
                  (addr,)) or _same_box(f["device_id"], f["tuner_count"])
    if have:
        _refresh(have["id"], info or {})
        return have["id"]
    with db.tx() as c:
        cur = c.execute("INSERT INTO tuner_boxes (address, found_by, enabled, added_at) "
                        "VALUES (?, ?, 1, ?)", (addr, found_by, int(time.time())))
        box_id = cur.lastrowid
    if info:
        _refresh(box_id, info)
    return box_id


def set_enabled(box_id: int, on: bool) -> None:
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET enabled = ? WHERE id = ?", (1 if on else 0, box_id))


def remove(box_id: int) -> bool:
    """Only a typed row goes. A found one would come back at the next look."""
    with db.tx() as c:
        cur = c.execute("DELETE FROM tuner_boxes WHERE id = ? AND found_by = 'typed'", (box_id,))
        return cur.rowcount > 0


def migrate_settings() -> None:
    """The old `tuner_url` and `tuner_detected` settings become rows.

    Runs at start and again whenever either setting changes, so an install
    with one box keeps working with nothing new to set.
    """
    typed = (db.get_setting("tuner_url") or "").strip()
    found = (db.get_setting("tuner_detected") or "").strip()
    # A hash, so the marker never carries an address into a report.
    seen = hashlib.sha1(f"{typed}|{found}".encode()).hexdigest()
    if (db.get_setting("tuner_migrated") or "") == seen:
        return
    db.set_setting("tuner_migrated", seen)
    if typed and normalise(typed):
        add_typed(typed)
    if found and normalise(found):
        addr = normalise(found)
        info, err = _probe(addr)
        if info:
            add_found(addr, "plex", info)
        elif not db.one("SELECT id FROM tuner_boxes WHERE address = ?", (addr,)):
            with db.tx() as c:
                c.execute("INSERT INTO tuner_boxes (address, found_by, enabled, added_at, error) "
                          "VALUES (?, 'plex', 1, ?, ?)", (addr, int(time.time()), err))
