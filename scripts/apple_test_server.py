#!/usr/bin/env python3
"""A CouchElephant for the Apple apps' clicking tests to talk to.

    scripts/apple_test_server.sh            # in a container, on port 8790

The Apple apps' XCUITest suites (kept in a separate repository) press real buttons in the
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
  - the fake Firestore on FIRESTORE_PORT (default 8792), so a report sent
    from the apps or the server lands there and never at the owner's.
    GET /test/boxes gives the garage box's and the spare box's addresses;
    POST /test/boxes/reset takes the spare box out again (the Settings test
    adds it by address).
    POST /test/tuners?n=1 says the tuner has one tuner (n=0 undoes it), for the
    over-tuners warning of a channels-per-game preview.
    POST /test/plexemail?v=a@b.c sets the email plex.tv gives for the account
    (empty undoes it), for the report screen's prefill.
    GET /test/reports lists what arrived (with the App Check header each
    carried, and the sealed bytes) and POST /test/reports/reset empties it;
  - with AWAY=1, Use away from home on: the same port also answers HTTPS
    with its own certificate, and GET /test/pin says the key's fingerprint,
    so the pinning tests can connect with it. Sign-in stays off, and the
    tests come from the local network, so the gate lets them in.
    GET /test/keyseen says how many requests carried the device key
    (?reset=1 starts the count again). GET /test/requests lists every request
    that carried a key, with the port it came to and a short tag of the key,
    so a test can tell one key from another. The same app also answers
    plain HTTP on ALIAS_PORT (default 8791), a second address for the same
    install, which is what a link to "another address" needs.

Everything it writes goes to a scratch folder, checked by the same guard the
test suite uses. Stop it and nothing is left.
"""
import hashlib
import os
import shutil
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
from tests import (  # noqa: E402
    demo_guide,
    demo_world,
    fake_firestore,
    fake_plex,
    fake_tuner,
    isolation,
)

isolation.assert_isolated()

from app import db, oneport, recorder, sync, tls, tunerbox, web  # noqa: E402

PORT = int(os.environ.get("PORT", "8790"))
ALIAS_PORT = int(os.environ.get("ALIAS_PORT", "8791"))
FIRESTORE_PORT = int(os.environ.get("FIRESTORE_PORT", "8792"))
AD_BREAK = (20.0, 50.0)


def main():
    import uvicorn
    plex_url, _ = fake_plex.start()
    fake_tuner.LINEUP[:] = [(vcn, call) for vcn, (call, _) in demo_guide.CHANNELS.items()]
    fake_tuner.STATE.name = "Living room box"
    tuner_url, _ = fake_tuner.start()
    # A second box, with one tuner held by something else (Plex), and a
    # third that is only started: a test adds it by address.
    _, _, garage = fake_tuner.start_box(device_id="FAKE0002", state=fake_tuner.State(),
                                        total=2, name="Garage box")
    garage.plex_holds = ["5.1"]
    spare_url, _, _ = fake_tuner.start_box(device_id="FAKE0003", state=fake_tuner.State(),
                                           total=2, name="Spare box")
    # Reports go to the fake, reachable from the simulator on its own port.
    fake_firestore.ANY_PROJECT = True
    fs_url, _ = fake_firestore.start("0.0.0.0", FIRESTORE_PORT)
    os.environ["COUCHELEPHANT_REPORT_FIRESTORE_URL"] = fs_url
    print(f"fake Firestore on port {FIRESTORE_PORT}", flush=True)

    db.init()
    for k, v in {"plex_url": plex_url, "plex_token": "demo", "dry_run": "0",
                 "auth_mode": "none", "timezone": "America/Chicago",
                 "tuner_url": tuner_url, "recorder_encoder": "software"}.items():
        db.set_setting(k, v)
    tunerbox.add_typed(garage.url)
    if AWAY:
        db.set_setting("away_mode", "1")
        pin = tls.ensure_own().fingerprint

        @web.app.get("/test/pin")
        def _pin():
            return {"fp": pin}
        print("away mode on, key fingerprint", pin, flush=True)

        # Test only: counts the requests that carried the device key, so a
        # test can say the key never reached an address it must not.
        keys_seen = []
        resets = []
        requests_seen = []

        @web.app.middleware("http")
        async def _watch_key(request, call_next):
            auth = request.headers.get("authorization")
            if auth and not request.url.path.startswith("/test/"):
                # The Top Shelf extension asks for the home row by itself, with
                # the key, to the origin it keeps. That is allowed, so the key
                # count skips it. requests_seen keeps it, so a test still sees
                # the key going to an address it must not.
                ua = request.headers.get("user-agent", "")
                if "topshelf" not in ua.lower():
                    keys_seen.append((request.url.scheme, request.url.path, time.time()))
                requests_seen.append({"port": request.url.port, "path": request.url.path,
                                      "key": hashlib.sha256(auth.encode()).hexdigest()[:8]})
            return await call_next(request)

        @web.app.get("/test/requests")
        def _requests(reset: int = 0):
            seen = list(requests_seen)
            if reset:
                requests_seen.clear()
            return {"requests": seen}

        @web.app.get("/test/keyseen")
        def _keyseen(reset: int = 0):
            seen = list(keys_seen)
            now = time.time()
            if reset:
                keys_seen.clear()
                resets.append(now)
            # The last few, each with how long ago it came, so a failure says
            # whether it was late from an earlier test or new.
            return {"count": len(seen),
                    "paths": [f"{s} {p} {now - t:.1f}s ago" for s, p, t in seen[-5:]],
                    "reset": f"{now - resets[-1]:.1f}s ago" if resets else "never"}
    ok, detail = sync.full_sync()
    if not ok:
        raise SystemExit(f"the demo guide would not sync: {detail}")
    print("guide:", detail, flush=True)
    demo_world.add_news()

    @web.app.get("/test/reports")
    def _reports():
        """Test only: every report the fake Firestore took, oldest first."""
        st = fake_firestore.STATE
        return {"reports": [{"id": k, "source": v["source"]["stringValue"],
                             "size": int(v["size"]["integerValue"]),
                             "appVersion": v["appVersion"]["stringValue"],
                             "sealed": v["sealed"]["bytesValue"],
                             "appCheck": st.appcheck.get(k)}
                            for k, v in st.docs.items()]}

    @web.app.post("/test/reports/reset")
    def _reports_reset():
        fake_firestore.STATE.reset()
        return {"ok": True}

    @web.app.post("/test/seed-pass")
    def _seed_pass(title: str, n: int = 3, lost: int = 0, section: str = "tv"):
        """Test only: a season pass with n bookings of ours ahead, from airings
        later in the demo week. With lost=1 the pass is deleted straight away,
        which leaves the bookings behind as the owner found them."""
        return demo_world.seed_pass(title, n, lost, section)

    @web.app.post("/test/seed-recording")
    def _seed_recording(title: str, gaps: int = 0):
        """Test only: a finished recording with this title, so a search for a
        recording has one whatever earlier tests deleted. It is two days old, so it is not in
        today's calendar, where the first item is the recording under way.
        With gaps=1 it carries one 12 second hole at 1:04:10, so the card shows its note."""
        now = int(time.time())
        with db.tx() as c:
            # One seeded recording at a time, so tests do not pile them up.
            c.execute("DELETE FROM recordings WHERE uid = ?", ("5eed" * 8,))
            c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                             channel_vcn, channel_title, begins_at, ends_at, gaps)
                         VALUES (?, 'recording', 'finished', ?, ?, 'shows', '9.1', ?, ?, ?, ?)""",
                      ("5eed" * 8, title, title, demo_guide.CHANNELS["9.1"][1],
                       now - 172800, now - 169200,
                       '[{"at": 3850, "seconds": 12}]' if gaps else None))
        return {"ok": True}

    _long_seed = [0]

    @web.app.post("/test/seed-long")
    def _seed_long(title: str = "Long Game", seconds: int = 240, position: int = 200,
                   delay: int = 30, adbreak: str = "",
                   ondemand: int = 0):
        """Test only: a finished recording of `seconds` that has been left at
        `position`, whose viewing copy is being made again, as a tidied-away
        one is on demand: for `delay` seconds the copy holds only its first 30
        seconds and no end tag, then the rest appears. `adbreak` "a,b" adds a break. `ondemand=1` makes it a recording in Plex with no
        viewing copy at all: its stream lists every segment and makes each when asked. A jump to a later place
        before then has nowhere to land. Returns the recording's id."""
        import subprocess
        uid = "10ad" * 8
        vdir = recorder.view_dir(uid)
        if ondemand:
            plex_file = os.path.join(SCRATCH, "long.ts")
            from app import ondemand
            ondemand.forget(uid)
            shutil.rmtree(recorder.rec_dir(uid), ignore_errors=True)
            if not os.path.exists(plex_file):
                subprocess.run([recorder.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                                "testsrc=size=320x180:rate=15", "-f", "lavfi", "-i",
                                "sine=frequency=440", "-t", str(seconds), "-c:v", "mpeg2video",
                                "-c:a", "mp2", "-f", "mpegts", plex_file], check=True)
            now = int(time.time())
            with db.tx() as c:
                c.execute("DELETE FROM recordings WHERE uid = ?", (uid,))
                cur = c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title,
                                       section, channel_vcn, channel_title, begins_at, ends_at,
                                       position, plex_path, moved_at)
                                   VALUES (?, 'recording', 'in_plex', ?, ?, 'shows', '9.1', ?, ?, ?, ?, ?, ?)""",
                                (uid, title, title, demo_guide.CHANNELS["9.1"][1],
                                 now - 172800, now - 172800 + seconds, position, plex_file, now - 86400))
                if adbreak:
                    a, b = (float(x) for x in adbreak.split(","))
                    c.execute("INSERT INTO ad_breaks (recording_id, start_s, end_s, final) "
                              "VALUES (?, ?, ?, 1)", (cur.lastrowid, a, b))
                    c.execute("UPDATE recordings SET ads_final = 1 WHERE id = ?", (cur.lastrowid,))
            return {"ok": True, "id": cur.lastrowid}
        src = os.path.join(vdir, "full.m3u8")
        # Made once (it takes a few seconds), then reused by every seed.
        if not os.path.exists(src) or f"seg{seconds // 6 - 1:05d}.ts" not in open(src).read():
            shutil.rmtree(vdir, ignore_errors=True)
            os.makedirs(vdir)
            subprocess.run([recorder.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=160x90:rate=10", "-f", "lavfi", "-i",
                            "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264",
                            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30",
                            "-c:a", "aac", "-f", "hls", "-hls_time", "6",
                            "-hls_playlist_type", "event",
                            "-hls_segment_filename", os.path.join(vdir, "seg%05d.ts"),
                            src], check=True)
        full = open(src, encoding="utf-8").read()
        # The first five segments (30 s), without the end tag.
        short, kept = [], 0
        for line in full.splitlines():
            if line.startswith("#EXTINF:"):
                kept += 1
            if kept > 5 or line.startswith("#EXT-X-ENDLIST"):
                break
            short.append(line)
        index = os.path.join(vdir, "index.m3u8")
        open(index, "w", encoding="utf-8").write("\n".join(short) + "\n")

        _long_seed[0] += 1
        mine = _long_seed[0]

        def _grow():
            time.sleep(delay)
            # An earlier seed's timer must not grow a later seed's copy.
            if _long_seed[0] == mine:
                shutil.copyfile(src, index)
        threading.Thread(target=_grow, daemon=True).start()
        now = int(time.time())
        with db.tx() as c:
            c.execute("DELETE FROM recordings WHERE uid = ?", (uid,))
            cur = c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                                   channel_vcn, channel_title, begins_at, ends_at, position)
                               VALUES (?, 'recording', 'finished', ?, ?, 'shows', '9.1', ?, ?, ?, ?)""",
                            (uid, title, title, demo_guide.CHANNELS["9.1"][1],
                             now - 172800, now - 172800 + seconds, position))
            if adbreak:
                a, b = (float(x) for x in adbreak.split(","))
                c.execute("INSERT INTO ad_breaks (recording_id, start_s, end_s, final) "
                          "VALUES (?, ?, ?, 1)", (cur.lastrowid, a, b))
                c.execute("UPDATE recordings SET ads_final = 1 WHERE id = ?", (cur.lastrowid,))
        return {"ok": True, "id": cur.lastrowid}

    from app.routes import passes as _pass_routes
    @web.app.get("/test/boxes")
    def _boxes():
        """Test only: the garage box's address (to add as a typed one) and
        the spare box's, which a test adds by hand."""
        return {"garage": garage.url, "spare": spare_url}

    @web.app.post("/test/boxes/reset")
    def _boxes_reset():
        """Test only: take the spare box out again."""
        for b in tunerbox.rows():
            if b["address"] == spare_url:
                tunerbox.remove(b["id"])
        return {"ok": True}

    _real_tuner_total = _pass_routes._tuner_total

    @web.app.post("/test/tuners")
    def _tuners(n: int = 0):
        """Test only: say the tuner has n tuners, so a pass that books two
        channels at once is over them. 0 puts the real count back."""
        _pass_routes._tuner_total = (lambda: n) if n else _real_tuner_total
        return {"ok": True, "tuners": n or _real_tuner_total()}

    from app import plextv as _plextv
    plex_email = {"v": ""}

    @web.app.post("/test/plexemail")
    def _plexemail(v: str = ""):
        """Test only: the email plex.tv would give for the account, for the
        report screen's prefill. Empty means plex.tv gives none."""
        plex_email["v"] = v
        return {"ok": True}

    _plextv.account_email = lambda token: plex_email["v"] or None

    @web.app.get("/test/pass-state")
    def _pass_state(title: str):
        """Test only: is there a pass for this title, and how many of its
        bookings are still set."""
        return {"pass": bool(db.one("SELECT 1 FROM passes WHERE series_title = ?", (title,))),
                "scheduled": db.one("SELECT COUNT(*) c FROM recordings WHERE show_title = ? "
                                    "AND state = 'scheduled'", (title,))["c"]}

    threading.Thread(target=demo_world.ticking, daemon=True).start()
    airing = demo_world.longest_on_now()
    if airing:
        rec_id = recorder.book(airing, trail_minutes=0)
        with db.tx() as c:
            c.execute("INSERT INTO ad_breaks (recording_id, start_s, end_s, final) "
                      "VALUES (?, ?, ?, 0)", (rec_id, *AD_BREAK))
        print(f"recording {rec_id}: {airing['title']} on {airing['channel_vcn']}, "
              f"ad break {AD_BREAK}", flush=True)

    print(f"CouchElephant for the Apple tests on port {PORT}, scratch {SCRATCH}", flush=True)
    config = uvicorn.Config(web.app, host="0.0.0.0", port=PORT, log_level="warning",
                            proxy_headers=False, server_header=False,
                            timeout_keep_alive=oneport.KEEP_ALIVE_SECONDS)
    if AWAY:
        # A second address for the same install: plain HTTP on its own port.
        alias = uvicorn.Server(uvicorn.Config(web.app, host="0.0.0.0", port=ALIAS_PORT, log_level="warning",
                                              proxy_headers=False, server_header=False,
                                              timeout_keep_alive=oneport.KEEP_ALIVE_SECONDS))
        threading.Thread(target=alias.run, daemon=True).start()
        oneport.serve(config)       # HTTP and HTTPS on the one port
    else:
        uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
