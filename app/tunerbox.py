"""The HDHomeRun boxes CouchElephant knows: the rows, and what each one says.

`tuner.py` is what the rest of the app calls. This file owns the table and the
cached answers from each box.
"""
import hashlib
import ipaddress
import logging
import socket
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit

import httpx

from . import db

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 2
LINEUP_TTL = 600            # a lineup is trusted this long
LINEUP_KEEP = 3600          # and a failed read keeps the last good one this long
STATUS_TTL = 5              # one tick asks each box at most once
STATUS_FAIL_TTL = 15        # a box that did not answer is left alone a while
LINEUP_BUDGET = 6           # all the lineup reads of one call share this many seconds
LINEUP_RETRY = 30           # a lineup that failed is asked again this soon
SEEN_EVERY = 60             # last_seen is written at most this often

_lock = threading.Lock()
_add_lock = threading.Lock()    # the check-then-insert of a typed row
_lineups: dict = {}
_statuses: dict = {}
_marked: dict = {}


def reset_cache() -> None:
    with _lock:
        _lineups.clear()
        _statuses.clear()
        _marked.clear()


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
    _merge_twins(f["device_id"], f["tuner_count"])


def _merge_twins(device_id, count) -> None:
    """Rows that turn out to be one box (same DeviceID and tuner count) become
    one. A typed row always stays; with none typed the lower id does."""
    if not device_id:
        return
    with db.tx() as c:
        twins = c.execute("SELECT id, found_by FROM tuner_boxes WHERE device_id = ? "
                          "AND tuner_count IS ? ORDER BY id", (device_id, count)).fetchall()
        if len(twins) < 2:
            return
        keep = {t["id"] for t in twins if t["found_by"] == "typed"} or {twins[0]["id"]}
        for t in twins:
            if t["id"] not in keep:
                c.execute("DELETE FROM tuner_boxes WHERE id = ?", (t["id"],))


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
    # The probe is slow, so a second add of the same address may have landed
    # meanwhile. Looked at again, and written, with nobody else in between.
    with _add_lock:
        have = db.one("SELECT id FROM tuner_boxes WHERE address = ? AND found_by = 'typed'",
                      (addr,))
        if have:
            return have["id"]
        same = _same_box(f.get("device_id"), f.get("tuner_count")) if info else None
        if same:
            # Found already (by Plex or the network): the typed address
            # replaces the found one, and the row keeps its place.
            with db.tx() as c:
                c.execute("UPDATE tuner_boxes SET address = ?, found_by = 'typed' WHERE id = ?",
                          (addr, same["id"]))
            box_id = same["id"]
        else:
            with db.tx() as c:
                cur = c.execute("INSERT INTO tuner_boxes (address, found_by, enabled, added_at, "
                                "error) VALUES (?, 'typed', 1, ?, ?)",
                                (addr, int(time.time()), err))
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


def _mark(box, error: str | None = None) -> None:
    """Note that a box answered, or why it did not. Written when it changes,
    and at most once a minute otherwise."""
    now = time.time()
    key = box["id"]
    with _lock:
        last = _marked.get(key)
        if last and last[1] == error and (error or now - last[0] < SEEN_EVERY):
            return
        _marked[key] = (now, error)
    with db.tx() as c:
        if error:
            c.execute("UPDATE tuner_boxes SET error = ? WHERE id = ?", (error, key))
        else:
            c.execute("UPDATE tuner_boxes SET last_seen = ?, error = NULL WHERE id = ?",
                      (int(now), key))


def _words(box, e) -> str:
    return f"The tuner at {box['address']} did not answer: {e}"


def _read_lineup(box) -> list:
    r = httpx.get(box["address"] + "/lineup.json", timeout=5)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise ValueError("not a lineup")
    return rows


def lineup(box) -> tuple[list[dict], bool]:
    """(channels, stale). Kept ten minutes. When a box stops answering its last
    good lineup is kept for an hour and marked stale. Never raises."""
    now = time.time()
    key = box["id"]
    with _lock:
        c = _lineups.get(key)
        if c and c["addr"] != box["address"]:
            c = None
        if c and now - c["tried_at"] < (LINEUP_RETRY if c["stale"] else LINEUP_TTL):
            return c["rows"], c["stale"]
    try:
        rows = _read_lineup(box)
    except (httpx.HTTPError, ValueError, OSError) as e:
        _mark(box, _words(box, e))
        with _lock:
            old = _lineups.get(key)
            keep = old["rows"] if old and old["addr"] == box["address"] \
                and now - old["good_at"] < LINEUP_KEEP else []
            _lineups[key] = {"addr": box["address"], "rows": keep, "stale": True,
                             "tried_at": now, "good_at": old["good_at"] if old else 0}
            return keep, True
    _mark(box)
    with _lock:
        _lineups[key] = {"addr": box["address"], "rows": rows, "stale": False,
                         "tried_at": now, "good_at": now}
    return rows, False


def lineups(boxes) -> dict:
    """{box id: (channels, stale)} for every box, read together. The reads
    share one time limit, so a hung box costs that and not its own timeout on
    top of every other box's. A box that missed the limit shows what was
    cached for it, stale, or nothing."""
    boxes = list(boxes)
    if len(boxes) < 2:
        return {b["id"]: lineup(b) for b in boxes}
    pool = ThreadPoolExecutor(max_workers=len(boxes))
    futs = {b["id"]: pool.submit(lineup, b) for b in boxes}
    wait(list(futs.values()), timeout=LINEUP_BUDGET)
    pool.shutdown(wait=False)       # a hung read finishes on its own, into the cache
    out = {}
    for b in boxes:
        f = futs[b["id"]]
        if f.done() and not f.exception():
            out[b["id"]] = f.result()
            continue
        with _lock:
            c = _lineups.get(b["id"])
            out[b["id"]] = (c["rows"] if c and c["addr"] == b["address"] else [], True)
    return out


def lineup_is_stale(box) -> bool:
    """True when the last read of this box's lineup failed. Looks only at what
    is cached, never asks the box."""
    with _lock:
        c = _lineups.get(box["id"])
        return bool(c and c["addr"] == box["address"] and c["stale"])


def _fetch_status(box):
    """(rows, dead). `rows` is None when the box has no /status.json (a 404, or
    an answer that is not JSON, like Threadfin's); `dead` is True when it did
    not answer at all."""
    try:
        r = httpx.get(box["address"] + "/status.json", timeout=2)
        r.raise_for_status()
        rows = r.json()
        return (rows if isinstance(rows, list) else None), False
    except httpx.TransportError:
        return None, True               # refused, unreachable or timed out
    except (httpx.HTTPError, ValueError):
        return None, False
    except OSError:
        return None, True


def statuses(boxes) -> dict:
    """{box id: tuner rows, or None when /status.json does not answer}. Boxes
    are asked together, so one that hangs costs two seconds, not two each."""
    now = time.time()
    out, need = {}, []
    with _lock:
        for b in boxes:
            c = _statuses.get(b["id"])
            if c and c["addr"] == b["address"] and \
                    now - c["at"] < (STATUS_TTL if c["rows"] is not None else STATUS_FAIL_TTL):
                out[b["id"]] = c["rows"]
            else:
                need.append(b)
    if need:
        with ThreadPoolExecutor(max_workers=len(need)) as pool:
            got = list(pool.map(_fetch_status, need))
        with _lock:
            for b, (rows, dead) in zip(need, got):
                _statuses[b["id"]] = {"addr": b["address"], "rows": rows, "at": now,
                                      "dead": dead}
                out[b["id"]] = rows
    return out


def unreachable(box) -> bool:
    """True when the last status read got no answer at all. Looks only at
    what is cached, never asks the box."""
    with _lock:
        c = _statuses.get(box["id"])
        return bool(c and c["addr"] == box["address"] and c.get("dead"))


def status(box) -> list[dict] | None:
    """One row per tuner, a busy one carrying `VctNumber`. None when the box
    has no /status.json (some emulators). Asked at most every few seconds."""
    return statuses([box])[box["id"]]


# --- finding boxes ---------------------------------------------------------

DISCOVER_ADDR = "255.255.255.255"
DISCOVER_PORT = 65001       # where every HDHomeRun listens for "who is there"


def discover_packet() -> bytes:
    """The HDHomeRun discover request: type 0x0002, any device type, any
    device id, then a CRC32 of all of it, low byte first."""
    payload = (b"\x01\x04" + b"\xff\xff\xff\xff"
               + b"\x02\x04" + b"\xff\xff\xff\xff")
    head = struct.pack(">HH", 0x0002, len(payload))
    return head + payload + struct.pack("<I", zlib.crc32(head + payload))


def broadcast(timeout: float = 1.5) -> list[str]:
    """Shout on the LAN and return the address of every box that answers.
    Raises OSError when the socket cannot broadcast."""
    found: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(discover_packet(), (DISCOVER_ADDR, DISCOVER_PORT))
        end = time.time() + timeout
        while (left := end - time.time()) > 0:
            s.settimeout(left)
            try:
                _, (ip, _port) = s.recvfrom(2048)
            except TimeoutError:
                break
            if ip not in found:
                found.append(ip)
    finally:
        s.close()
    return found


def _on_docker_bridge() -> bool:
    """True when the only way out is a Docker bridge, where a broadcast never
    reaches the real network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))     # sends nothing, only picks the route
        ip = ipaddress.ip_address(s.getsockname()[0])
    except (OSError, ValueError):
        return False
    finally:
        s.close()
    return ip in ipaddress.ip_network("172.16.0.0/12")


def _plex_address(uri: str) -> str:
    """The address Plex reaches a box on, as we would: http, no port 80."""
    uri = (uri or "").strip()
    if not uri.startswith("http"):
        return ""
    a = normalise(uri)
    return a[:-3] if a.endswith(":80") else a


def _touch(box) -> int | None:
    """Ask a row's box who it is now, and note it. Returns the id if it answered."""
    info, err = _probe(box["address"])
    if info:
        _refresh(box["id"], info)
        return box["id"]
    with db.tx() as c:
        c.execute("UPDATE tuner_boxes SET error = ? WHERE id = ?", (err, box["id"]))
    return None


def discover(plex=None) -> dict:
    """Find every box Plex names and every box that answers on the network,
    then refresh what every row says about itself."""
    reached: set[int] = set()
    if plex is not None:
        try:
            devices = plex.grabber_devices()
        except Exception as e:
            log.warning("tuner discovery: Plex would not list its devices: %s", e)
            devices = []
        for d in devices:
            addr = _plex_address(d.get("uri"))
            info, _ = _probe(addr) if addr else (None, None)
            if info and info.get("LineupURL"):
                box_id = add_found(addr, "plex", info)
                if box_id:
                    reached.add(box_id)
    ips, ok = [], True
    try:
        ips = broadcast()
    except OSError as e:
        log.info("tuner discovery: cannot broadcast: %s", e)
        ok = False
    if ok and not ips and _on_docker_bridge():
        ok = False
    for ip in ips:
        addr = f"http://{ip}"
        info, _ = _probe(addr)
        if info and info.get("LineupURL"):
            box_id = add_found(addr, "network", info)
            if box_id:
                reached.add(box_id)
    db.set_setting("tuner_broadcast_ok", "1" if ok else "0")
    rest = [b for b in rows() if b["id"] not in reached]
    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as pool:
            reached.update(i for i in pool.map(_touch, rest) if i)
    return {"found": len(reached), "can_find_on_network": ok}
