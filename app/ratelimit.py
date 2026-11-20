"""Sign-in limits. Five failures in ten minutes locks that key for a minute;
each further lockout doubles it, up to an hour. A key is an address or an
account, so guessing from many addresses at one account still runs dry."""
import collections
import ipaddress
import threading
import time

WINDOW = 600
FAILS = 5
FIRST_LOCK = 60
MAX_LOCK = 3600
# A key left alone this long starts over from the short lock.
FORGET = 24 * 3600
# The oldest key goes when the table is full, so made-up names cannot grow it.
MAX_KEYS = 10000

_lock = threading.Lock()
_keys: collections.OrderedDict = collections.OrderedDict()


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    with _lock:
        _keys.clear()


def addr_key(addr) -> str:
    """An address as a key. An IPv6 one is a whole /64, which is one household,
    so changing the last 64 bits buys no new tries."""
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return "addr:" + str(addr)
    if ip.version == 6:
        return "addr:" + str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return "addr:" + str(ip)


def _left(key, now) -> float:
    s = _keys.get(key)
    return max(0.0, s["until"] - now) if s else 0.0


def _lock_if_full(s, now) -> None:
    if len(s["fails"]) >= FAILS and s["until"] <= now:
        s["level"] += 1
        s["until"] = now + min(FIRST_LOCK * 2 ** (s["level"] - 1), MAX_LOCK)
        s["fails"] = []


def _entry(key, now):
    s = _keys.get(key)
    if s and now - s["last"] > FORGET:
        s = None
    if not s:
        s = _keys[key] = {"fails": [], "until": 0.0, "level": 0, "last": now}
    _keys.move_to_end(key)
    s["fails"] = [t for t in s["fails"] if now - t < WINDOW]
    return s


def _trim(now) -> None:
    """Drop the oldest keys past the cap, but never one that is locked: a flood
    of made-up names must not push a real lock out of the table."""
    if len(_keys) <= MAX_KEYS:
        return
    for k in list(_keys):
        if len(_keys) <= MAX_KEYS:
            break
        if _keys[k]["until"] <= now:
            del _keys[k]


def _hit(key, now) -> None:
    s = _entry(key, now)
    s["last"] = now
    s["fails"].append(now)
    _lock_if_full(s, now)
    _trim(now)


def check(key: str) -> float:
    """Seconds still locked, 0 when free."""
    with _lock:
        return _left(key, _now())


def fail(key: str) -> None:
    with _lock:
        _hit(key, _now())


def reserve(keys) -> float:
    """Take an attempt on every key, or answer how long to wait. The attempt
    is counted before it is judged, so parallel requests cannot all slip past
    one check. Five are allowed in the window; the next one finds the key full
    and locks it. A right answer gives its attempt back with `refund`."""
    with _lock:
        now = _now()
        wait = 0.0
        # Look before adding anything: a locked caller must not grow the table.
        for k in keys:
            s = _keys.get(k)
            if s and now - s["last"] <= FORGET:
                _lock_if_full(s, now)
                wait = max(wait, _left(k, now))
        if wait:
            return wait
        for k in keys:
            s = _entry(k, now)
            s["last"] = now
            s["fails"].append(now)
        _trim(now)
        return 0.0


def refund(key: str) -> None:
    with _lock:
        s = _keys.get(key)
        if s and s["fails"]:
            s["fails"].pop()


def ok(key: str) -> None:
    with _lock:
        _keys.pop(key, None)


def say(seconds: float) -> str:
    return "Too many tries. Wait a minute." if seconds <= 60 else (
        f"Too many tries. Wait {int(seconds // 60) + 1} minutes.")
