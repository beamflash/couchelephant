"""Keeps the recent server log in memory so a problem report can include it.
Lines are raw here; callers must scrub them before they leave the server."""

import collections
import logging
import threading
import time

_MAX_RECORDS = 5000
_LOGGERS = ("couchelephant", "uvicorn.error")

_lock = threading.Lock()
_buf: collections.deque = collections.deque(maxlen=_MAX_RECORDS)  # (created, line)


def _has_other_handler(name: str) -> bool:
    """True if a handler besides ours sits in the logger's chain."""
    lg = logging.getLogger(name)
    while lg:
        if any(h is not _handler for h in lg.handlers):
            return True
        if not lg.propagate:
            break
        lg = lg.parent
    return False


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            line = f"{stamp} {record.levelname} {record.name}: {msg}"
            with _lock:
                _buf.append((record.created, line))
            # our handler must not silence the stderr fallback
            if record.levelno >= logging.lastResort.level and not _has_other_handler(record.name):
                logging.lastResort.handle(record)
        except Exception:
            self.handleError(record)


_handler = _RingHandler(logging.INFO)


def install() -> None:
    """Attach the handler once; safe to call again."""
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        if _handler not in lg.handlers:
            lg.addHandler(_handler)
        # an unset level would inherit root's WARNING and drop INFO
        if lg.level == logging.NOTSET:
            lg.setLevel(logging.INFO)


def recent(max_lines: int = 2000, max_age: int = 900, now: float | None = None) -> list[str]:
    """Newest max_lines lines from the last max_age seconds, oldest first."""
    cutoff = (time.time() if now is None else now) - max_age
    with _lock:
        kept = [line for created, line in _buf if created >= cutoff]
    return kept[-max_lines:]


def clear() -> None:
    with _lock:
        _buf.clear()
