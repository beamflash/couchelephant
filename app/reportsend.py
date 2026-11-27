"""Sending issue reports to the owner's Firestore, sealed. A report that
cannot be sent waits in a small queue, still sealed, and goes out later."""
import base64
import logging
import secrets
import threading
import time
from typing import TypedDict

import httpx

from . import db, demo, reportbuild, reportconfig
from .version import VERSION

log = logging.getLogger("couchelephant")

TIMEOUT = 8
DAY = 86400
LIMIT_PER_DAY = 10
QUEUE_MAX = 20

_lock = threading.RLock()


class SendResult(TypedDict):
    ok: bool
    sent: bool
    queued: bool
    report_id: str
    reason: str | None


def _now() -> int:
    return int(time.time())


def _result(report_id="", ok=False, sent=False, queued=False, reason=None) -> SendResult:
    return {"ok": ok, "sent": sent, "queued": queued,
            "report_id": report_id, "reason": reason}


def install_id() -> str:
    """Random, made once. Nothing about the machine goes into it."""
    with _lock:
        cur = db.get_setting("report_install_id")
        if cur:
            return cur
        new = secrets.token_hex(16)
        db.set_setting("report_install_id", new)
        return new


def queue_size() -> int:
    return db.query("SELECT COUNT(*) AS n FROM report_queue")[0]["n"]


def _rfc3339(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _post(cfg, report_id, blob, source, app_version, created) -> str:
    """'sent', 'junk' (drop it) or 'retry' (keep it)."""
    url = (f"{cfg.base_url}/v1/projects/{cfg.project}/databases/(default)"
           f"/documents/reports")
    body = {"fields": {
        "reportId": {"stringValue": report_id},
        "sealed": {"bytesValue": base64.b64encode(blob).decode()},
        "size": {"integerValue": str(len(blob))},
        "created": {"timestampValue": _rfc3339(created)},
        "source": {"stringValue": source or ""},
        "appVersion": {"stringValue": app_version or ""},
        "keyId": {"stringValue": cfg.key_id},
        "installId": {"stringValue": install_id()},
    }}
    try:
        r = httpx.post(url, params={"documentId": report_id, "key": cfg.api_key},
                       json=body, timeout=TIMEOUT)
    except Exception as e:
        # Includes InvalidURL and UnsupportedProtocol, so submit never raises.
        # The class only: a message could carry the url and its key.
        log.info("report %s not sent: %s", report_id, type(e).__name__)
        return "retry"
    if r.status_code in (200, 409):
        return "sent"
    if r.status_code == 429 or r.status_code >= 500:
        log.info("report %s not sent: HTTP %s", report_id, r.status_code)
        return "retry"
    log.warning("report %s dropped: HTTP %s", report_id, r.status_code)
    return "junk"


def _enqueue(report_id, blob, source, app_version, created):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO report_queue "
                  "(report_id, blob, source, app_version, created) VALUES (?,?,?,?,?)",
                  (report_id, blob, source, app_version, created))
        # Oldest out when there are too many.
        c.execute("DELETE FROM report_queue WHERE report_id IN ("
                  "SELECT report_id FROM report_queue ORDER BY created, rowid "
                  "LIMIT MAX(0, (SELECT COUNT(*) FROM report_queue) - ?))",
                  (QUEUE_MAX,))


def flush_queue() -> int:
    """Send what is waiting, oldest first. Stops at the first failure."""
    cfg = reportconfig.config()
    if cfg is None:
        return 0
    n = 0
    with _lock:
        rows = db.query("SELECT * FROM report_queue ORDER BY created, rowid")
        for r in rows:
            out = _post(cfg, r["report_id"], bytes(r["blob"]), r["source"],
                        r["app_version"], r["created"] or _now())
            if out == "retry":
                break
            with db.tx() as c:
                c.execute("DELETE FROM report_queue WHERE report_id = ?",
                          (r["report_id"],))
            if out == "sent":
                n += 1
    return n


def _sent_recently(now: int) -> int:
    return db.query("SELECT COUNT(*) AS n FROM report_sent WHERE ts > ?",
                    (now - DAY,))[0]["n"]


def submit(report: dict, source: str) -> SendResult:
    rid = report.get("reportId", "")
    if demo.enabled():
        # The public demo never sends a report anywhere, whatever is configured.
        return _result(rid, reason="demo")
    cfg = reportconfig.config()
    if cfg is None:
        return _result(rid, reason="not_configured")
    with _lock:
        flush_queue()
        now = _now()
        if _sent_recently(now) >= LIMIT_PER_DAY:
            return _result(rid, reason="limit")
        try:
            blob = reportbuild.pack(report, public=cfg.public_key)
        except reportbuild.ReportTooLarge:
            return _result(rid, reason="too_large")
        app_version = (report.get("app") or {}).get("version") or VERSION
        with db.tx() as c:
            c.execute("DELETE FROM report_sent WHERE ts <= ?", (now - DAY,))
            c.execute("INSERT INTO report_sent (ts) VALUES (?)", (now,))
        out = _post(cfg, rid, blob, source, app_version, now)
        if out == "sent":
            return _result(rid, ok=True, sent=True)
        if out == "junk":
            return _result(rid, reason="rejected")
        _enqueue(rid, blob, source, app_version, now)
        start_flusher()
        return _result(rid, ok=True, queued=True)


FLUSH_EVERY = 15 * 60
_flusher: threading.Thread | None = None


def _flush_loop():
    global _flusher
    while True:
        time.sleep(FLUSH_EVERY)
        try:
            flush_queue()
        except Exception as e:
            log.warning("report queue flush failed: %s", type(e).__name__)
        with _lock:
            try:
                empty = queue_size() == 0
            except Exception as e:
                log.warning("report queue size failed: %s", type(e).__name__)
                empty = False
            if empty:
                _flusher = None
                return


def start_flusher() -> None:
    """A 15 minute retry, alive only while something is waiting."""
    global _flusher
    with _lock:
        if _flusher is not None or queue_size() == 0:
            return
        _flusher = threading.Thread(target=_flush_loop, daemon=True,
                                    name="report-flush")
        _flusher.start()
