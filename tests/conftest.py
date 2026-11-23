"""Fixtures. Every one of them is scratch, and the guard proves it."""
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The environment has to be set before the app imports, because db.py reads
# these at module level. Doing it in a fixture would be too late.
_ROOT = tempfile.mkdtemp(prefix="ce-test-")
os.environ["COUCHELEPHANT_DB"] = os.path.join(_ROOT, "couchelephant.db")
# Anything that asks for the data folder (the own certificate) gets scratch.
os.environ["COUCHELEPHANT_DATA"] = os.path.join(_ROOT, "data")
os.environ["COUCHELEPHANT_AUTH_DB"] = os.path.join(_ROOT, "auth.db")
os.environ["COUCHELEPHANT_LOGOS"] = os.path.join(_ROOT, "logos")
os.environ["COUCHELEPHANT_RECORDINGS"] = os.path.join(_ROOT, "recordings")
os.environ["COUCHELEPHANT_LIBRARY"] = os.path.join(_ROOT, "library")
# Not the real ffmpeg: no codecs to install, and nothing to wait for.
os.environ["COUCHELEPHANT_FFMPEG"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fake_ffmpeg.py")
# No comskip unless a test brings its stand-in, so a comskip installed on the
# machine running the suite is never started by it.
# Updates off unless a test brings a fake feed; never the real GitHub.
os.environ["COUCHELEPHANT_RELEASES_URL"] = ""
os.environ["COUCHELEPHANT_COMSKIP"] = os.path.join(_ROOT, "no-comskip")
# Never the owner's real Firestore: a closed port, and a test that wants a
# Firestore brings the fake. The shipped values are blanked per test below.
os.environ["COUCHELEPHANT_REPORT_FIRESTORE_URL"] = "http://127.0.0.1:9"

from tests import fake_plex, fake_tuner, isolation  # noqa: E402

isolation.assert_isolated()                      # before anything is imported

from app import (  # noqa: E402
    adskip,
    auth,
    db,
    ratelimit,
    recorder,
    reportconfig,  # noqa: E402
    sync,
)

# The shipped report settings, kept for the test that checks them.
SHIPPED_REPORT_CONFIG = {k: getattr(reportconfig, k)
                         for k in ("PROJECT", "API_KEY", "PUBLIC_KEY_B64", "KEY_ID")}


@pytest.fixture(autouse=True)
def no_real_reports(request, monkeypatch):
    """Reports are "not set up" unless a test sets the env to the fake. Only
    a `live` test (opt in with COUCHELEPHANT_REPORT_LIVE=1) gets the real one."""
    if request.node.get_closest_marker("live"):
        monkeypatch.setenv("COUCHELEPHANT_REPORT_FIRESTORE_URL", reportconfig.DEFAULT_URL)
        return
    for k in SHIPPED_REPORT_CONFIG:
        monkeypatch.setattr(reportconfig, k, "")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def plex_url():
    url, stop = fake_plex.start()
    os.environ["COUCHELEPHANT_TEST_PLEX"] = url
    isolation.assert_isolated()
    yield url
    stop()


@pytest.fixture(autouse=True)
def clean_db(plex_url):
    """A fresh database for every test.

    Dropping and recreating rather than reusing, because a test that leaves a
    pass behind changes what the next one sees, and a suite that only passes in
    order is worth very little.
    """
    isolation.assert_isolated()
    # No worker from the last test may still be writing, and no file of its
    # may be left for this one to find.
    recorder.shutdown()
    adskip.shutdown()
    for name in ("COUCHELEPHANT_RECORDINGS", "COUCHELEPHANT_LIBRARY"):
        shutil.rmtree(os.environ[name], ignore_errors=True)
        os.makedirs(os.environ[name])
    db.init()
    conn = db.connect()
    # Order would otherwise matter, and a new table would break the wipe the
    # first time somebody added one. Turning the constraints off for the
    # duration says what is meant: start from nothing.
    conn.executescript("PRAGMA foreign_keys=OFF")
    for table in _tables(conn):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.executescript("PRAGMA foreign_keys=ON")
    a = auth._con()
    a.executescript("DELETE FROM users; DELETE FROM sessions; "
                    "DELETE FROM email_map; DELETE FROM prefs;")
    a.commit()
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "test-token")
    db.set_setting("dry_run", "0")
    ratelimit.reset()
    db.set_setting("auth_mode", "none")
    # The guide is drawn from the server's configured timezone and positioned
    # by the browser's clock. They have to agree, or the grid asks for a window
    # a day away from the data. Both are pinned to UTC for the suite; the
    # browser side is pinned in tests/ui/conftest.py.
    db.set_setting("timezone", "UTC")
    # The suite's scratch install has the recorder on, which now makes it the
    # default. Every test written before that books with Plex and says so by
    # saying nothing, so Plex is pinned here and the tests about the default
    # set it themselves.
    db.set_setting("recorder_default", "plex")
    # And marked as chosen, or the app's own start-up would move it to auto.
    db.set_setting("recorder_default_auto", "1")
    fake_plex.STATE.reset()
    yield


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]


@pytest.fixture(scope="session")
def tuner_url():
    url, stop = fake_tuner.start()
    yield url
    stop()


@pytest.fixture
def tuner(tuner_url):
    """The fake HDHomeRun, set as the tuner, with every tuner free."""
    fake_tuner.STATE.reset()
    db.set_setting("tuner_url", tuner_url)
    db.set_setting("recorder_encoder", "software")
    yield fake_tuner.STATE
    recorder.shutdown()


@pytest.fixture
def plex(plex_url):
    from app.plex import Plex
    p = Plex(plex_url, "test-token")
    yield p
    p.close()


@pytest.fixture
def synced(plex):
    """A database with the fake server's guide already pulled in."""
    ok, detail = sync.full_sync()
    assert ok, detail
    return detail


@pytest.fixture
def client():
    """The app, driven in-process. No network, no port."""
    from fastapi.testclient import TestClient

    from app.web import app
    with TestClient(app, base_url="http://testserver") as c:
        # Same-origin by default, so the guard does not reject every call.
        c.headers.update({"Origin": "http://testserver", "Host": "testserver"})
        yield c
