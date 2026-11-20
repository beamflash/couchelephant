#!/usr/bin/env python3
"""A CouchElephant for the Apple apps' clicking tests to talk to.

    scripts/apple_test_server.sh            # in a container, on port 8790

The XCUITest suites in `clients/apple/UITests` press real buttons in the
simulator: they tune live TV, change channel from the guide over the
picture, record from it, and skip an ad. None of that may reach a real Plex,
a real tuner or real recordings, so this runs the app with everything it
talks to replaced:

  - the invented week of television in `tests/demo_guide.py`, on the fake
    Plex server;
  - the fake HDHomeRun, sending a moving test pattern per channel (real
    MPEG-2 video made by ffmpeg, so the player has something to play);
  - sign-in off, so a simulator is let in without a code;
  - one recording already under way, with an ad break 20 seconds in,
    for the Skip ad test;
  - with AWAY=1, Use away from home on: the same port also answers HTTPS
    with its own certificate, and GET /test/pin says the key's fingerprint,
    so the pinning tests can connect with it. Sign-in stays off, and the
    tests come from the local network, so the gate lets them in.

Everything it writes goes to a scratch folder, checked by the same guard the
test suite uses. Stop it and nothing is left.
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = tempfile.mkdtemp(prefix="ce-test-apple-")
os.environ["COUCHELEPHANT_DB"] = os.path.join(SCRATCH, "couchelephant.db")
os.environ["COUCHELEPHANT_AUTH_DB"] = os.path.join(SCRATCH, "auth.db")
os.environ["COUCHELEPHANT_LOGOS"] = os.path.join(SCRATCH, "logos")
os.environ["COUCHELEPHANT_RECORDINGS"] = os.path.join(SCRATCH, "recordings")
os.environ["COUCHELEPHANT_LIBRARY"] = os.path.join(SCRATCH, "library")
os.environ["COUCHELEPHANT_NO_SYNC_LOOP"] = "1"
os.environ["COUCHELEPHANT_DEMO_GUIDE"] = "1"
os.environ["COUCHELEPHANT_COMSKIP"] = os.path.join(SCRATCH, "no-comskip")
os.environ["FAKE_TUNER_VIDEO"] = "1"
AWAY = os.environ.get("AWAY") == "1"
if AWAY:
    os.environ["COUCHELEPHANT_DATA"] = os.path.join(SCRATCH, "data")
os.makedirs(os.environ["COUCHELEPHANT_RECORDINGS"])
os.makedirs(os.environ["COUCHELEPHANT_LIBRARY"])
# The demo week starts at the top of this hour, so something is on now.
os.environ.setdefault("COUCHELEPHANT_FAKE_ANCHOR", str(int(time.time()) // 3600 * 3600))

sys.path.insert(0, ROOT)
from tests import demo_guide, fake_plex, fake_tuner, isolation  # noqa: E402

isolation.assert_isolated()

from app import db, oneport, recorder, sync, tls, web  # noqa: E402

PORT = int(os.environ.get("PORT", "8790"))
AD_BREAK = (20.0, 50.0)


def _longest_on_now():
    """What is on now and runs on longest, so the recording made of it lasts
    through a whole test run."""
    now = int(time.time())
    return db.one("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams
                     FROM airings a JOIN programs p ON p.guid = a.program_guid
                     WHERE a.begins_at <= ? AND a.ends_at > ? AND a.drm = 0
                     ORDER BY a.ends_at DESC LIMIT 1""", (now, now))


def _ticking():
    """The recorder's own loop, without the ones that reach other servers."""
    while True:
        try:
            recorder.tick()
        except Exception as e:  # noqa: BLE001
            print("recorder:", e, flush=True)
        time.sleep(3)


def main():
    import uvicorn
    plex_url, _ = fake_plex.start()
    fake_tuner.LINEUP[:] = [(vcn, call) for vcn, (call, _) in demo_guide.CHANNELS.items()]
    tuner_url, _ = fake_tuner.start()

    db.init()
    for k, v in {"plex_url": plex_url, "plex_token": "demo", "dry_run": "0",
                 "auth_mode": "none", "timezone": "America/Chicago",
                 "tuner_url": tuner_url, "recorder_encoder": "software"}.items():
        db.set_setting(k, v)
    if AWAY:
        db.set_setting("away_mode", "1")
        pin = tls.ensure_own().fingerprint

        @web.app.get("/test/pin")
        def _pin():
            return {"fp": pin}
        print("away mode on, key fingerprint", pin, flush=True)
    ok, detail = sync.full_sync()
    if not ok:
        raise SystemExit(f"the demo guide would not sync: {detail}")
    print("guide:", detail, flush=True)

    threading.Thread(target=_ticking, daemon=True).start()
    airing = _longest_on_now()
    if airing:
        rec_id = recorder.book(airing, trail_minutes=0)
        with db.tx() as c:
            c.execute("INSERT INTO ad_breaks (recording_id, start_s, end_s, final) "
                      "VALUES (?, ?, ?, 0)", (rec_id, *AD_BREAK))
        print(f"recording {rec_id}: {airing['title']} on {airing['channel_vcn']}, "
              f"ad break {AD_BREAK}", flush=True)

    print(f"CouchElephant for the Apple tests on port {PORT}, scratch {SCRATCH}", flush=True)
    config = uvicorn.Config(web.app, host="0.0.0.0", port=PORT, log_level="warning",
                            proxy_headers=False, server_header=False)
    if AWAY:
        oneport.serve(config)       # HTTP and HTTPS on the one port
    else:
        uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
