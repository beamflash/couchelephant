#!/usr/bin/env python3
"""The public CouchElephant demo, for the App Store reviewer.

    DEMO_CODE=k7m3xq9ntw4h2p python scripts/demo_server.py

The same app, with everything it talks to replaced by the invented world the
Apple tests use (tests/demo_world.py): an invented guide on a fake Plex, a fake
tuner sending a test pattern, finished recordings with ad breaks, a scheduled
recording and a season pass. Unlike scripts/apple_test_server.py there is no
fake Firestore and no /test routes, sign-in is on, and the app's demo mode
(app/demo.py) locks everything that is not the apps' own use.

It resets its content to the seeded state when it starts and every night at
DEMO_RESET_HOUR (container local time, 4 by default), unless someone has used it
in the last half hour (then it asks again every hour). Devices that were let in
and the reviewer's account stay across a reset. Nothing outside the
data folder is written.

Settings, all from the environment:

  DEMO_CODE            the review code (required, never in the source): 12 to 16
                       lowercase letters and digits, none of 0 o 1 l i
  DEMO_DATA            where everything lives (default /data)
  DEMO_MEDIA           where the viewing copies are made once (default DATA/media)
  DEMO_TRUSTED_PROXIES the reverse proxy's own address (the compose file gives Caddy
                       172.29.0.2 and trusts only that; the default, Docker's
                       bridge networks 172.16.0.0/12, is for trying it by hand)
  DEMO_TIMEZONE        the timezone the guide is shown in (default America/Los_Angeles)
  DEMO_TUNERS          streams at once, live and recording together (default 2)
  DEMO_RESET_HOUR      hour of the nightly reset, 0 to 23 (default 4)
  PORT                 default 8710
"""
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DEMO_DATA") or os.environ.get("COUCHELEPHANT_DATA") or "/data"
MEDIA = os.environ.get("DEMO_MEDIA") or os.path.join(DATA, "media")

MEDIA_ONLY = "--media-only" in sys.argv
if MEDIA_ONLY:
    # The image build makes the viewing copies and nothing else.
    os.environ.setdefault("DEMO_CODE", "kkkkkkkkkkkk")
if not os.environ.get("DEMO_CODE"):
    raise SystemExit("DEMO_CODE is not set. The demo needs a review code, from the "
                     "environment, and will not start without one.")
# Typed with a TV remote: lowercase letters and digits, none that look alike
# (no 0, o, 1, l or i), 12 to 16 of them.
if not re.fullmatch(r"[a-hjkmnp-z2-9]{12,16}", os.environ["DEMO_CODE"]):
    raise SystemExit("DEMO_CODE must be 12 to 16 lowercase letters and digits, without "
                     "0, o, 1, l or i (it is typed with a TV remote). Make one with: "
                     "python3 -c \"import secrets;print(''.join(secrets.choice("
                     "'abcdefghjkmnpqrstuvwxyz23456789') for _ in range(14)))\"")

os.makedirs(DATA, exist_ok=True)
os.environ.update({
    "COUCHELEPHANT_DEMO": "1",
    "COUCHELEPHANT_DATA": DATA,
    "COUCHELEPHANT_DB": os.path.join(DATA, "couchelephant.db"),
    "COUCHELEPHANT_AUTH_DB": os.path.join(DATA, "auth.db"),
    "COUCHELEPHANT_LOGOS": os.path.join(DATA, "logos"),
    "COUCHELEPHANT_RECORDINGS": os.path.join(DATA, "recordings"),
    "COUCHELEPHANT_LIBRARY": os.path.join(DATA, "library"),
    # No guide, announce, update or backup loops: they reach other servers.
    "COUCHELEPHANT_NO_SYNC_LOOP": "1",
    "COUCHELEPHANT_RELEASES_URL": "",
    # Reports are never sent from here; a closed port as well as the code.
    "COUCHELEPHANT_REPORT_FIRESTORE_URL": "http://127.0.0.1:9",
    "COUCHELEPHANT_COMSKIP": os.path.join(DATA, "no-comskip"),
    "COUCHELEPHANT_DEMO_GUIDE": "1",
    "COUCHELEPHANT_DEMO_INVENTED": "1",
    # A day and a half ahead, so the guide is never empty between resets.
    "COUCHELEPHANT_DEMO_GUIDE_HOURS": "36",
    "FAKE_TUNER_VIDEO": "1",
})
# A small picture, so a small server can make a couple at once.
os.environ.setdefault("FAKE_TUNER_SIZE", "640x360")
os.environ.setdefault("FAKE_TUNER_RATE", "24")
os.environ.setdefault("FAKE_TUNER_BITRATE", "1M")
os.environ.setdefault("COUCHELEPHANT_FAKE_ANCHOR", str(int(time.time()) // 3600 * 3600))
os.makedirs(os.environ["COUCHELEPHANT_RECORDINGS"], exist_ok=True)
os.makedirs(os.environ["COUCHELEPHANT_LIBRARY"], exist_ok=True)

sys.path.insert(0, ROOT)
from app import db, demo, oneport, web  # noqa: E402
from tests import demo_world  # noqa: E402

PORT = int(os.environ.get("PORT", "8710"))
RESET_HOUR = int(os.environ.get("DEMO_RESET_HOUR", "4"))


def settings(plex_url, tuner_url):
    return {
        "plex_url": plex_url, "plex_token": "demo", "dry_run": "0",
        "auth_mode": "local", "away_mode": "1",
        "timezone": os.environ.get("DEMO_TIMEZONE", "America/Los_Angeles"),
        "tuner_url": tuner_url, "recorder_encoder": "software",
        "trusted_proxies": os.environ.get("DEMO_TRUSTED_PROXIES", "172.16.0.0/12"),
        # A live channel nobody has touched for five minutes (the shortest the
        # setting takes) gives its tuner back, so an abandoned session cannot
        # hold one of the two streams for long.
        "live_pause_minutes": "5",
    }


def seconds_until_reset(now=None):
    """Until the next RESET_HOUR o'clock on the container's own clock."""
    now = now or datetime.now()
    at = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return (at - now).total_seconds()


def nightly(do_reset):
    while True:
        time.sleep(seconds_until_reset())
        # Not while someone is looking: a reviewer with a key in the last half
        # hour keeps the demo as it is, and it asks again every hour.
        while demo.recently_used():
            print("reset waits: the demo is in use", flush=True)
            time.sleep(3600)
        try:
            do_reset()
            print("reset done", flush=True)
        except Exception as e:  # noqa: BLE001
            print("reset failed:", e, flush=True)
            time.sleep(60)


def main():
    if MEDIA_ONLY:
        demo_world.ensure_media(MEDIA)
        print("viewing copies made in", MEDIA, flush=True)
        return
    import uvicorn
    plex_url, tuner_url = demo_world.start_fakes(int(os.environ.get("DEMO_TUNERS", "2")))
    db.init()
    conf = settings(plex_url, tuner_url)

    def do_reset():
        demo_world.reset(conf, MEDIA, demo.REVIEW_USER, demo.code())

    do_reset()
    print(f"demo seeded, resets daily at {RESET_HOUR:02d}:00, port {PORT}", flush=True)
    threading.Thread(target=demo_world.ticking, kwargs={"each": demo.enforce_limits},
                     daemon=True).start()
    threading.Thread(target=nightly, args=(do_reset,), daemon=True).start()
    uvicorn.run(web.app, host="0.0.0.0", port=PORT, log_level="warning",
                proxy_headers=False, server_header=False,
                timeout_keep_alive=oneport.KEEP_ALIVE_SECONDS)


if __name__ == "__main__":
    main()
