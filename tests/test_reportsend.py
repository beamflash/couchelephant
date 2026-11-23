import base64
import json
import os
import types
from pathlib import Path

import httpx
import pytest

from app import db, reportbuild, reportconfig, reportsend, sealing
from app.reportscrub import Scrubber
from tests import fake_firestore
from tests.fake_firestore import STATE

VEC = json.loads((Path(__file__).parent / "data" / "seal_vector.json").read_text())
PRIV = base64.b64decode(VEC["private"])
PUB_B64 = VEC["public"]


@pytest.fixture
def fs(client, monkeypatch):
    url, stop = fake_firestore.start()
    STATE.reset()
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PUBLIC_KEY", PUB_B64)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PROJECT", fake_firestore.PROJECT)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_API_KEY", fake_firestore.API_KEY)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_FIRESTORE_URL", url)
    yield STATE
    stop()


def _report(message="my secret complaint zebra", rid=None, **kw):
    args = dict(source="web", app={"version": "9.9.9"}, trigger={"kind": "manual"},
                app_log=["log line quokka"], server=None, server_note=None,
                message=message, email="who@example.org", scrub=Scrubber(salt=b"s"),
                report_id=rid)
    args.update(kw)
    return reportbuild.build_report(**args)


def test_submit_sends_one_document_with_expected_fields_and_no_cleartext(fs):
    r = reportsend.submit(_report(), "web")
    assert r["ok"] and r["sent"] and not r["queued"]
    assert len(fs.docs) == 1
    doc = next(iter(fs.docs.values()))
    assert set(doc) == {"reportId", "sealed", "size", "created", "source",
                        "appVersion", "keyId", "installId"}
    assert doc["keyId"]["stringValue"] == VEC["keyId"]
    assert doc["appVersion"]["stringValue"] == "9.9.9"
    for body in fs.bodies:
        for word in (b"zebra", b"quokka", b"who@example.org"):
            assert word not in body
    sealed = base64.b64decode(doc["sealed"]["bytesValue"])
    assert b"zebra" in __import__("gzip").decompress(sealing.open_sealed(sealed, PRIV))


def test_document_id_is_report_id(fs):
    rid = "ab" * 16
    reportsend.submit(_report(rid=rid), "web")
    assert list(fs.docs) == [rid]
    assert fs.requests[0][2]["documentId"] == rid


def test_same_report_twice_is_one_document(fs):
    rep = _report()
    assert reportsend.submit(rep, "web")["sent"]
    r = reportsend.submit(rep, "web")
    assert r["ok"] and r["sent"] and not r["queued"]
    assert len(fs.docs) == 1


def test_failure_queues_and_flush_sends_later(fs):
    fs.fail_next(503)
    r = reportsend.submit(_report(), "web")
    assert r["ok"] and r["queued"] and not r["sent"]
    assert reportsend.queue_size() == 1 and not fs.docs
    assert reportsend.flush_queue() == 1
    assert reportsend.queue_size() == 0 and len(fs.docs) == 1


def test_429_and_network_error_queue(fs, monkeypatch):
    fs.fail_next(429)
    assert reportsend.submit(_report(), "web")["queued"]

    def boom(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", boom)
    assert reportsend.submit(_report(), "web")["queued"]
    assert reportsend.queue_size() == 2


def test_queue_keeps_20_drops_oldest(fs, monkeypatch):
    t = [1_000_000]
    monkeypatch.setattr(reportsend, "_now", lambda: t[0])
    monkeypatch.setattr(reportsend, "LIMIT_PER_DAY", 100)
    ids = []
    for i in range(22):
        t[0] += 1
        rid = f"{i:032x}"
        ids.append(rid)
        reportsend._enqueue(rid, b"x", "web", "1", t[0])
    left = [r["report_id"] for r in db.query("SELECT report_id FROM report_queue")]
    assert len(left) == 20
    assert ids[0] not in left and ids[1] not in left and ids[21] in left


def test_rate_limit_10_per_day_then_refused_not_queued(fs):
    for _ in range(10):
        assert reportsend.submit(_report(), "web")["sent"]
    r = reportsend.submit(_report(), "web")
    assert not r["ok"] and r["reason"] == "limit"
    assert reportsend.queue_size() == 0
    assert len(fs.docs) == 10


def test_rate_limit_resets_after_a_day(fs, monkeypatch):
    t = [1_000_000]
    monkeypatch.setattr(reportsend, "_now", lambda: t[0])
    for _ in range(10):
        reportsend.submit(_report(), "web")
    assert reportsend.submit(_report(), "web")["reason"] == "limit"
    t[0] += 86401
    assert reportsend.submit(_report(), "web")["sent"]


def test_junk_400_is_dropped_not_queued(fs):
    fs.fail_next(400)
    r = reportsend.submit(_report(), "web")
    assert not r["ok"] and not r["queued"] and r["reason"] == "rejected"
    assert reportsend.queue_size() == 0


def test_junk_in_queue_is_dropped_on_flush(fs):
    fs.fail_next(503)
    reportsend.submit(_report(), "web")
    fs.fail_next(403)
    assert reportsend.flush_queue() == 0
    assert reportsend.queue_size() == 0


def test_not_configured_queues_nothing_and_says_so(client, monkeypatch):
    monkeypatch.setattr(reportconfig, "PROJECT", "")
    monkeypatch.delenv("COUCHELEPHANT_REPORT_PROJECT", raising=False)
    assert reportconfig.config() is None
    r = reportsend.submit(_report(), "web")
    assert not r["ok"] and r["reason"] == "not_configured"
    assert reportsend.queue_size() == 0


def test_install_id_is_stable_and_not_derived_from_anything(client):
    a = reportsend.install_id()
    assert len(a) == 32 and int(a, 16) >= 0
    assert reportsend.install_id() == a
    with db.tx() as c:
        c.execute("DELETE FROM settings WHERE key = 'report_install_id'")
    assert reportsend.install_id() != a


def test_queue_blob_on_disk_is_sealed(fs):
    fs.fail_next(503)
    reportsend.submit(_report(), "web")
    row = db.connect().execute("SELECT blob FROM report_queue").fetchone()
    blob = bytes(row["blob"])
    assert b"zebra" not in blob
    import gzip
    assert b"zebra" in gzip.decompress(sealing.open_sealed(blob, PRIV))


def test_fake_refuses_reads(fs):
    r = httpx.get(os.environ["COUCHELEPHANT_REPORT_FIRESTORE_URL"] + "/anything")
    assert r.status_code == 403


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("COUCHELEPHANT_REPORT_LIVE") != "1",
                    reason="set COUCHELEPHANT_REPORT_LIVE=1 to hit the real Firestore")
def test_live_send(client):
    assert reportconfig.config() is not None
    r = reportsend.submit(_report(message="live check, ignore"), "web")
    assert r["ok"] and r["sent"]


def test_flusher_only_starts_when_something_waits(fs, monkeypatch):
    started = []

    class T:
        def __init__(self, **kw):
            pass

        def start(self):
            started.append(1)
    # Only reportsend's view of threading: patching threading.Thread itself
    # also stops asyncio's executor shutdown in the test client, which hangs.
    monkeypatch.setattr(reportsend, "threading", types.SimpleNamespace(Thread=T))
    monkeypatch.setattr(reportsend, "_flusher", None)
    reportsend.start_flusher()
    assert not started
    reportsend._enqueue("c" * 32, b"x", "web", "1", 1)
    reportsend.start_flusher()
    assert started == [1]
    monkeypatch.setattr(reportsend, "_flusher", None)


def test_submit_never_raises_on_bad_url(fs, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_REPORT_FIRESTORE_URL", "nope://x")
    r = reportsend.submit(_report(), "web")
    assert r["ok"] and r["queued"]


def test_flush_loop_survives_a_db_error(fs, monkeypatch):
    calls = {"n": 0}

    def size():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db gone")
        return 0
    sleeps = iter([None, None])

    def fake_sleep(_):
        if next(sleeps, "stop") == "stop":
            raise SystemExit
    monkeypatch.setattr(reportsend.time, "sleep", fake_sleep)
    monkeypatch.setattr(reportsend, "queue_size", size)
    monkeypatch.setattr(reportsend, "flush_queue", lambda: 0)
    monkeypatch.setattr(reportsend, "_flusher", object())
    reportsend._flush_loop()
    assert calls["n"] == 2 and reportsend._flusher is None


def test_shipped_config_is_the_owners_project_and_is_blanked_in_tests():
    from tests.conftest import SHIPPED_REPORT_CONFIG as shipped
    assert shipped["PROJECT"] == "beamflash-ce-reports-open"
    assert shipped["API_KEY"].startswith("AIza")
    assert sealing.key_id(base64.b64decode(shipped["PUBLIC_KEY_B64"])) == shipped["KEY_ID"]
    assert reportconfig.PROJECT == "" and reportconfig.API_KEY == ""
    assert os.environ["COUCHELEPHANT_REPORT_FIRESTORE_URL"] == "http://127.0.0.1:9"
