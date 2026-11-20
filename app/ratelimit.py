"""Sign-in limits. Five failures in ten minutes locks that key for a minute;
each further lockout doubles it, up to an hour. A key is an address or an
account, so guessing from many addresses at one account still runs dry."""
import threading
import time

WINDOW = 600
FAILS = 5
FIRST_LOCK = 60
MAX_LOCK = 3600
# A key left alone this long starts over from the short lock.
FORGET = 24 * 3600

_lock = threading.Lock()
_keys: dict = {}


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    with _lock:
        _keys.clear()


def check(key: str) -> float:
    """Seconds still locked, 0 when free."""
    with _lock:
        s = _keys.get(key)
        if not s:
            return 0.0
        return max(0.0, s["until"] - _now())


def fail(key: str) -> None:
    now = _now()
    with _lock:
        s = _keys.get(key)
        if s and now - s["last"] > FORGET:
            s = None
        if not s:
            s = _keys[key] = {"fails": [], "until": 0.0, "level": 0, "last": now}
        s["last"] = now
        s["fails"] = [t for t in s["fails"] if now - t < WINDOW]
        s["fails"].append(now)
        if len(s["fails"]) >= FAILS and s["until"] <= now:
            s["level"] += 1
            s["until"] = now + min(FIRST_LOCK * 2 ** (s["level"] - 1), MAX_LOCK)
            s["fails"] = []
        if len(_keys) > 5000:       # a flood of made-up names must not grow forever
            for k in [k for k, v in _keys.items() if now - v["last"] > WINDOW and v["until"] < now]:
                del _keys[k]


def ok(key: str) -> None:
    with _lock:
        _keys.pop(key, None)


def say(seconds: float) -> str:
    return "Too many tries. Wait a minute." if seconds <= 60 else (
        f"Too many tries. Wait {int(seconds // 60) + 1} minutes.")
