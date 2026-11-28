"""Builds a problem report from the server's state and packs it for the owner.

The server part only ever reads what reportscrub allows, and every string goes
through a Scrubber that was told this server's own secrets (make_scrubber).
"""
import gzip
import json
import os
import platform
import re
import secrets
import shutil
import time

from . import db, demo, health, logring, paths, recorder, sealing, tunerbox
from .reportscrub import HOST_SETTINGS, SECRET_SETTINGS_ALL, Scrubber
from .version import VERSION

SOURCES = ("web", "ios", "ipados", "tvos", "server")


def _process_start() -> float:
    """When this process started: /proc where there is one, else import time."""
    try:
        with open("/proc/self/stat") as f:
            # Fields after the ")" that ends the name; starttime is the 20th.
            ticks = int(f.read().rpartition(")")[2].split()[19])
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return time.time() - (up - ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, AttributeError):
        return time.time()


_STARTED = _process_start()


class ReportTooLarge(Exception):
    pass


def make_scrubber() -> Scrubber:
    """A Scrubber that knows this server's secrets, because patterns alone
    miss a bare token.

    Device keys, poll secrets and session tokens are stored only as hashes
    (tv_devices.key_hash/poll_hash, sessions.token_hash), so no raw value of
    those exists to pass. What is stored raw is passed: the SECRET_SETTINGS_ALL
    settings, the pending pairing codes, and each notification destination's
    webhook and token.
    """
    s = db.all_settings()
    secrets_ = [s.get(k) or "" for k in SECRET_SETTINGS_ALL]
    for r in db.query("SELECT code FROM tv_devices WHERE code IS NOT NULL"):
        secrets_.append(r["code"])
    for r in db.query("SELECT webhook, token FROM destinations"):
        secrets_ += [r["webhook"] or "", r["token"] or ""]
    hosts = [s.get(k) or "" for k in HOST_SETTINGS]
    hosts += [b["address"] for b in db.query("SELECT address FROM tuner_boxes")]
    return Scrubber(known_secrets=[x for x in secrets_ if len(x) >= 4],
                    known_hosts=[h for h in hosts if h])


def _encoder() -> str:
    """What the recorder would use, from the cached probe only. Asking
    recorder.encoder() could run ffmpeg for up to 30 seconds."""
    if (db.get_setting("recorder_encoder") or "auto").lower() == "software":
        return "software"
    probed = recorder._vaapi
    if probed is None:
        return "unknown (not probed yet)"
    return "vaapi" if probed else "software"


def _disk() -> dict:
    d = paths.recordings_dir()
    # The folder may not exist yet; ask the nearest one that does.
    while not d.exists() and d != d.parent:
        d = d.parent
    u = shutil.disk_usage(d)
    return {"free_bytes": u.free, "total_bytes": u.total}


def server_part(scrub: Scrubber) -> dict:
    s = db.all_settings()
    why = recorder.why_unavailable()
    boxes = tunerbox.rows()
    passes = []
    for r in db.query("SELECT kind, team_name, series_title, label, enabled "
                      "FROM passes ORDER BY id"):
        title = r["series_title"] or r["team_name"] or r["label"] or ""
        passes.append({"kind": r["kind"], "title": scrub.text(title),
                       "enabled": bool(r["enabled"])})
    fails = []
    for r in db.query("SELECT title, begins_at, error FROM recordings "
                      "WHERE state = 'failed' ORDER BY COALESCE(begins_at, 0) DESC, "
                      "id DESC LIMIT 10"):
        fails.append({"title": scrub.text(r["title"] or ""), "time": r["begins_at"],
                      "error": scrub.text(r["error"] or "")})
    part = {
        "version": VERSION,
        "uptime_seconds": int(time.time() - _STARTED),
        "os": scrub.text(platform.platform()),
        "container": paths._in_container(),
        "python": platform.python_version(),
        "log": [scrub.text(x) for x in logring.recent()],
        "settings": scrub.settings(s),
        # How many boxes and what kind, never where they are.
        "tuner": {"count": len(boxes),
                  "detected": any(b["found_by"] != "typed" for b in boxes),
                  "boxes": [{"model": scrub.text(b["model"] or ""), "found_by": b["found_by"],
                             "enabled": bool(b["enabled"]), "tuners": b["tuner_count"]}
                            for b in boxes]},
        "recorder": {"available": why is None,
                     "why_unavailable": scrub.text(why) if why else None,
                     "encoder": scrub.text(_encoder())},
        "passes": passes,
        "schedule_failures": fails,
        "notices": [scrub.text(n["code"]) for n in health.open_notices()],
        "disk": _disk(),
    }
    if demo.enabled():
        # The apps send their own reports straight to the owner, so a report
        # that came from the public demo can be told apart and thrown away.
        part["demo"] = True
    return part


def _walk(v, scrub):
    if isinstance(v, str):
        return scrub.text(v)
    if isinstance(v, list):
        return [_walk(x, scrub) for x in v]
    if isinstance(v, dict):
        return {scrub.text(k) if isinstance(k, str) else k: _walk(x, scrub)
                for k, x in v.items()}
    return v


def build_report(*, source: str, app: dict, trigger: dict, app_log: list[str],
                 server: dict | None, server_note: str | None, message: str,
                 email: str, scrub: Scrubber, report_id: str | None = None,
                 now: int | None = None) -> dict:
    if source not in SOURCES:
        raise ValueError(f"bad source: {source!r}")
    if report_id is not None and not re.fullmatch(r"[0-9a-f]{32}", report_id):
        raise ValueError("report_id must be 32 lower-case hex characters")
    return {
        "v": 1,
        "reportId": report_id or secrets.token_hex(16),
        "created": int(time.time()) if now is None else now,
        "source": source,
        "app": _walk(app, scrub),
        "trigger": _walk(trigger, scrub),
        "appLog": [scrub.text(x) for x in app_log],
        # The server part is already scrubbed; a second pass is harmless.
        "server": _walk(server, scrub) if server is not None else None,
        "serverNote": scrub.text(server_note) if server_note else server_note,
        "message": message,
        "email": email,
    }


def _logs(report: dict):
    """The two droppable lists, as (holder, key) pairs."""
    out = []
    if isinstance(report.get("appLog"), list):
        out.append((report, "appLog"))
    srv = report.get("server")
    if isinstance(srv, dict) and isinstance(srv.get("log"), list):
        out.append((srv, "log"))
    return out


def _seal(report: dict, public: bytes) -> bytes:
    raw = json.dumps(report, separators=(",", ":")).encode()
    return sealing.seal(gzip.compress(raw), public)


def pack(report: dict, cap: int = 900_000, public: bytes | None = None) -> bytes:
    """gzip(JSON), sealed. Over the cap, the oldest log lines go first, from
    whichever log is longer. Trigger and message are never touched."""
    if public is None:
        raise ValueError("a public key is needed to seal a report")
    # Our own copy, so the caller's report keeps its logs.
    rep = json.loads(json.dumps(report))
    _had_logs = any(h[k] for h, k in _logs(rep))
    while True:
        blob = _seal(rep, public)
        if len(blob) <= cap:
            return blob
        logs = [(h, k) for h, k in _logs(rep) if h[k]]
        if not logs:
            raise ReportTooLarge(
                "trigger and message alone exceed the cap" if not _had_logs
                else "report too large even without logs")
        h, k = max(logs, key=lambda hk: sum(len(x) for x in hk[0][hk[1]]))
        # A fifth at a time, so a few MB does not take thousands of seals.
        drop = max(1, len(h[k]) // 5)
        del h[k][:drop]
