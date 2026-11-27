"""Demo mode: the public copy for the App Store reviewer (app/demo.py)."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from app import announce, auth, db, demo, exposure, paths, ratelimit, reportsend
from app.web import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = "review-code-for-tests"
PROXY = "172.29.0.2"
OUT = "8.8.8.8"


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    monkeypatch.setattr(exposure, "_adapters", lambda: [])
    monkeypatch.setattr(exposure, "_route_files", lambda: [])
    exposure.clear_cache()
    ratelimit.reset()
    demo.reset_guesses()
    yield
    exposure.clear_cache()
    ratelimit.reset()
    demo.reset_guesses()


@pytest.fixture
def on(monkeypatch):
    """The demo as scripts/demo_server.py sets it up: sign-in and away on, a
    reviewer account, Caddy's network trusted."""
    monkeypatch.setenv("COUCHELEPHANT_DEMO", "1")
    monkeypatch.setenv("DEMO_CODE", CODE)
    db.set_setting("auth_mode", "local")
    db.set_setting("away_mode", "1")
    db.set_setting("trusted_proxies", "172.29.0.0/24")
    return auth.create_user(demo.REVIEW_USER, CODE, role="user")


def visitor(addr=OUT, proto="https", via_proxy=True):
    """A client from the internet. Through the proxy it arrives from the
    proxy's address with the real one in X-Forwarded-For, as with Caddy."""
    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (PROXY if via_proxy else addr, 4321)}
        await app(scope, receive, send)
    c = TestClient(shim, base_url=f"{proto}://testserver", follow_redirects=False)
    c.headers.update({"Host": "demo.test", "Origin": f"{proto}://demo.test"})
    if via_proxy:
        c.headers.update({"X-Forwarded-For": addr, "X-Forwarded-Proto": proto})
    return c


def pair(c):
    """What an app does, and what the reviewer does on the web page."""
    asked = c.post("/api/tv/pair", data={"name": "Reviewer's iPhone"})
    assert asked.status_code == 200, asked.text
    got = asked.json()
    ok = c.post("/demo/pair", data={"review_code": CODE, "code": got["code"]})
    assert ok.status_code == 200, ok.text
    poll = c.post("/api/tv/pair/poll", data={"secret": got["secret"]}).json()
    assert poll["approved"]
    return poll["key"]


def paired():
    c = visitor()
    c.headers["Authorization"] = "Bearer " + pair(c)
    return c


def signed_in(c):
    r = c.post("/login", data={"username": demo.REVIEW_USER, "password": CODE})
    assert r.status_code == 303 and "ce_session" in c.cookies, r.text
    return c


# ---------- it is off unless asked for ----------

def test_off_by_default_nothing_changes():
    assert not demo.enabled()
    c = visitor()
    assert c.get("/demo").status_code == 404
    assert c.post("/demo/pair", data={"review_code": "x", "code": "ABCDEF"}).status_code == 404
    # The lock does nothing when the switch is off.
    db.set_setting("auth_mode", "none")
    assert visitor(via_proxy=False, addr="192.168.1.5").post(
        "/settings/test", data={"plex_url": "http://127.0.0.1:1", "plex_token": "x"}
    ).status_code != 403


# ---------- no test routes, no fake Firestore ----------

def test_there_are_no_test_routes(on):
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/test")]
    c = signed_in(visitor())
    for path in ("/test/reports", "/test/pin", "/test/keyseen", "/test/requests"):
        assert c.get(path).status_code in (403, 404)
    assert c.post("/test/seed-pass?title=x").status_code in (403, 404)


def test_the_entry_point_has_no_test_routes_or_fake_firestore():
    src = open(os.path.join(ROOT, "scripts", "demo_server.py")).read()
    assert "fake_firestore" not in src
    assert "/test/" not in src
    # The same goes for what it shares with the Apple tests' server.
    world = open(os.path.join(ROOT, "tests", "demo_world.py")).read()
    assert "fake_firestore" not in world and "/test/" not in world


# ---------- sign-in is required ----------

def test_nothing_opens_without_the_review_sign_in(on):
    c = visitor()
    assert c.get("/api/tv/hello").json()["pairing_required"] is True
    for path in ("/api/tv/home", "/api/tv/guide", "/api/tv/recordings", "/api/grid"):
        assert c.get(path).status_code in (401, 303), path
    assert c.get("/hls/" + "a" * 32 + "/master.m3u8").status_code in (401, 303)
    assert c.get("/logo/9.1").status_code in (401, 303)


def test_a_wrong_review_code_does_not_let_an_app_in(on):
    c = visitor()
    got = c.post("/api/tv/pair", data={"name": "x"}).json()
    r = c.post("/demo/pair", data={"review_code": "not-the-code", "code": got["code"]})
    assert r.status_code == 403
    assert c.post("/api/tv/pair/poll", data={"secret": got["secret"]}).json()["approved"] is False


def test_the_review_code_comes_from_the_environment(on, monkeypatch):
    c = visitor()
    got = c.post("/api/tv/pair", data={"name": "x"}).json()
    monkeypatch.setenv("DEMO_CODE", "")
    assert c.post("/demo/pair", data={"review_code": "", "code": got["code"]}).status_code == 403
    monkeypatch.setenv("DEMO_CODE", "another-code-entirely")
    assert c.post("/demo/pair", data={"review_code": CODE, "code": got["code"]}).status_code == 403


def test_guessing_the_review_code_is_limited(on):
    c = visitor()
    got = c.post("/api/tv/pair", data={"name": "x"}).json()
    seen = [c.post("/demo/pair", data={"review_code": f"guess-{i}", "code": got["code"]}
                   ).status_code for i in range(demo.GUESS_BUDGET + 2)]
    assert seen[:demo.GUESS_BUDGET] == [403] * demo.GUESS_BUDGET
    assert seen[-1] == 429
    # Past the budget even the right one waits.
    assert c.post("/demo/pair", data={"review_code": CODE, "code": got["code"]}
                  ).status_code == 429


def test_a_right_code_still_works_after_some_wrong_ones_from_the_same_address(on):
    c = visitor()
    for i in range(demo.GUESS_BUDGET - 1):
        assert c.post("/api/tv/pair/code", data={"code": f"wrong{i}"}).status_code == 403
    assert c.post("/api/tv/pair/code", data={"code": CODE}).status_code == 200
    # And again: a right answer is not a strike.
    assert c.post("/api/tv/pair/code", data={"code": CODE}).status_code == 200


def test_the_guess_budget_is_per_address(on):
    c = visitor()
    for i in range(demo.GUESS_BUDGET + 1):
        c.post("/api/tv/pair/code", data={"code": f"wrong{i}"})
    assert visitor(addr="9.9.9.9").post("/api/tv/pair/code", data={"code": CODE}
                                        ).status_code == 200


def test_an_app_pairs_and_then_reads_the_guide(on):
    c = paired()
    assert c.get("/api/tv/hello").json()["paired"] is True
    assert c.get("/api/tv/recordings").status_code == 200
    # The same request without the key is still refused.
    assert visitor().get("/api/tv/recordings").status_code in (401, 303)


def test_the_web_sign_in_works_with_the_review_code(on):
    c = signed_in(visitor())
    assert c.get("/guide").status_code == 200
    bad = visitor().post("/login", data={"username": "reviewer", "password": "nope-nope"})
    assert bad.status_code == 303 and "error" in bad.headers["location"]


# ---------- reports do not go out ----------

def test_a_report_is_never_sent_and_says_so(on, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a demo must not reach the network for a report")
    monkeypatch.setattr(reportsend.httpx, "post", boom)
    monkeypatch.setattr(reportsend.httpx, "request", boom, raising=False)
    c = signed_in(visitor())
    assert c.post("/api/report/consent", data={"agree": "1"}).status_code == 200
    r = c.post("/api/report/send", content=json.dumps(
        {"message": "hello", "email": "", "trigger": {"kind": "support"}}),
        headers={"Content-Type": "application/json"})
    body = r.json()
    assert body["ok"] is False and "demo" in body["error"].lower()
    assert reportsend.queue_size() == 0
    assert reportsend.submit({"reportId": "r1"}, "web")["reason"] == "demo"


def test_the_server_part_of_a_report_says_it_is_the_demo(on):
    from app import reportbuild
    assert reportbuild.server_part(reportbuild.make_scrubber())["demo"] is True


# ---------- the admin side is locked ----------

LOCKED_WRITES = [
    ("/settings", {}), ("/settings/test", {"plex_url": "http://169.254.169.254", "plex_token": "x"}),
    ("/settings/auth", {"mode": "none"}), ("/settings/away", {"on": "0"}),
    ("/settings/plex/signin", {}), ("/sync", {}), ("/api/import", {}),
    ("/api/backups/jobs", {}), ("/api/backingstore/test", {}),
    ("/api/update/apply", {}), ("/api/tv/devices/approve", {"code": "ABCDEF"}),
    ("/api/tv/devices/1/delete", {}), ("/api/tv/settings", {}),
    ("/api/tv/settings/test", {"plex_url": "http://127.0.0.1:1"}),
    ("/api/tv/recordings/1/delete", {}), ("/settings/notify/save", {}),
    ("/settings/users/1/delete", {}), ("/welcome", {}), ("/setup", {}),
]


@pytest.mark.parametrize("path,form", LOCKED_WRITES)
def test_a_signed_in_visitor_cannot_change_settings(on, path, form):
    c = signed_in(visitor())
    assert c.post(path, data=form).status_code == 403, path


@pytest.mark.parametrize("path,form", LOCKED_WRITES)
def test_a_paired_app_cannot_change_settings_either(on, path, form):
    c = paired()
    assert c.post(path, data=form).status_code == 403, path


@pytest.mark.parametrize("path", ["/settings", "/partial/settings", "/api/export",
                                  "/api/backups/jobs", "/api/away/check", "/api/connect.svg",
                                  "/api/tv/devices", "/api/update", "/api/recorder/library-suggest"])
def test_the_admin_pages_cannot_be_read(on, path):
    for c in (signed_in(visitor()), paired()):
        r = c.get(path)
        # The settings pages send a visitor to a short page that says what to do.
        assert r.status_code in (403, 404) or (
            r.status_code == 303 and r.headers["location"] == "/demo"), (path, r.status_code)


def test_what_the_apps_do_is_not_locked(on):
    c = paired()
    with db.tx() as t:
        t.execute("INSERT INTO channels (vcn, title) VALUES ('9.1', 'Nine')")
    assert c.post("/api/tv/favorites/add", data={"vcn": "9.1"}).status_code == 200
    assert c.post("/api/tv/favorites/remove", data={"vcn": "9.1"}).status_code == 200
    for path in ("/api/tv/record", "/api/tv/record/cancel", "/api/tv/passes/create",
                 "/api/tv/live/9.1", "/api/tv/live/3/stop", "/api/tv/recordings/3/position"):
        assert not demo.blocked("POST", path), path
    assert demo.blocked("POST", "/api/tv/recordings/3/delete")
    assert demo.blocked("POST", "/api/tv/passes/lost/../../settings")


# ---------- behind the proxy ----------

def _recording():
    with db.tx() as c:
        c.execute("INSERT INTO recordings (uid, kind, state, title, channel_vcn, begins_at, "
                  "ends_at, created_at) VALUES (?, 'recording', 'finished', 'T', '9.1', ?, ?, ?)",
                  ("a" * 32, 1, 2, 1))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_links_follow_the_scheme_the_proxy_reports(on):
    rid = _recording()
    c = paired()
    got = c.get(f"/api/tv/recordings/{rid}").json()["recording"]
    assert got["playlist"].startswith("https://demo.test/hls/"), got["playlist"]
    assert got["art"].startswith("https://demo.test/"), got["art"]


def test_the_scheme_is_believed_only_from_the_trusted_proxy(on):
    rid = _recording()
    key = pair(visitor())
    # The same header from anywhere else changes nothing.
    stranger = visitor(via_proxy=False, proto="http")
    stranger.headers.update({"X-Forwarded-Proto": "https", "Authorization": f"Bearer {key}"})
    got = stranger.get(f"/api/tv/recordings/{rid}").json()["recording"]
    assert got["playlist"].startswith("http://demo.test/hls/"), got["playlist"]


def test_pairing_works_over_the_proxys_plain_http_hop(on):
    # Caddy speaks http to the container: the app's away rule would otherwise
    # say "Pair this device at home first".
    c = visitor(proto="http")
    assert c.post("/api/tv/pair", data={"name": "x"}).status_code == 200


# ---------- the world, and the nightly reset ----------

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _run(code, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COUCHELEPHANT_")}
    env.update(PYTHONPATH=ROOT, DEMO_CODE="k7m3xq9ntw4h2p", DEMO_DATA=str(tmp_path / "data"),
               DEMO_MEDIA=str(tmp_path / "media"), DEMO_MEDIA_SCALE="0.02")
    if shutil.which("ffmpeg"):
        env["COUCHELEPHANT_FFMPEG"] = shutil.which("ffmpeg")
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip().splitlines()[-1]


RESET_SCRIPT = """
    import json, os, sys, threading, time
    sys.argv = ["demo_server.py"]
    sys.path.insert(0, "scripts")
    import demo_server as s
    from tests import demo_world
    from app import auth, db, demo, recorder
    if os.environ.get("STUB_MEDIA"):
        def stub(media_dir, spec):
            out = os.path.join(media_dir, spec[0])
            os.makedirs(out, exist_ok=True)
            open(os.path.join(out, "seg00000.ts"), "wb").write(b"\\x47" + b"\\0" * 187)
            open(os.path.join(out, "index.m3u8"), "w").write(
                "#EXTM3U\\n#EXT-X-TARGETDURATION:4\\n#EXTINF:4.0,\\nseg00000.ts\\n#EXT-X-ENDLIST\\n")
        demo_world._make_one = stub
    plex, tuner = demo_world.start_fakes(2)
    db.init()
    conf = s.settings(plex, tuner)

    def look():
        n = lambda q: db.one(q)["n"]
        return {
            "finished": n("SELECT COUNT(*) n FROM recordings WHERE state='finished'"),
            "scheduled": n("SELECT COUNT(*) n FROM recordings WHERE state='scheduled'"),
            "passes": n("SELECT COUNT(*) n FROM passes"),
            "breaks": n("SELECT COUNT(*) n FROM ad_breaks"),
            "airings": n("SELECT COUNT(*) n FROM airings") > 100,
            "logos": n("SELECT COUNT(*) n FROM channels WHERE logo_path != ''"),
            "users": [u["username"] for u in auth.list_users()],
            "resume": n("SELECT COUNT(*) n FROM recordings WHERE position > 0"),
            "viewable": all(os.path.exists(recorder.playlist_path(r["uid"]))
                            for r in db.query("SELECT uid FROM recordings WHERE state='finished'")),
        }

    demo_world.reset(conf, s.MEDIA, "reviewer", os.environ["DEMO_CODE"])
    first = look()
    seed = int(db.get_setting(demo.SEED_KEY))
    # A reviewer was let in, signed in on the web, and made a mess.
    now = int(time.time())
    with db.tx() as c:
        c.execute("DELETE FROM recordings WHERE state='finished'")
        c.execute("DELETE FROM passes")
        c.execute("INSERT INTO tv_devices (name, key_hash, approved_at, created_at, last_seen) "
                  "VALUES ('kept', 'abc', ?, ?, ?)", (now, now, now))
        c.execute("INSERT INTO tv_devices (name, code, created_at) VALUES ('pending', 'ZZZZZZ', ?)",
                  (now,))
        c.execute("INSERT INTO favorite_channels (vcn) VALUES ('9.1')")
    db.set_setting("timezone", "UTC")
    uid = auth._con().execute("SELECT id FROM users").fetchone()[0]
    token = auth.create_session(uid)
    # Is the demo ever without sign-in while it resets?
    bad = []
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            if db.get_setting("auth_mode") != "local" or db.get_setting("away_mode") != "1":
                bad.append("settings read open")
            if not demo.RESETTING.is_set() and False:
                bad.append("x")
            time.sleep(0.001)
    t = threading.Thread(target=watch); t.start()
    during = []
    t2 = threading.Thread(target=lambda: [during.append(demo.RESETTING.is_set()) or time.sleep(0.01)
                                         for _ in range(200)])
    t2.start()
    demo_world.reset(conf, s.MEDIA, "reviewer", os.environ["DEMO_CODE"])
    stop.set(); t.join(); t2.join()
    second = look()
    print(json.dumps({
        "first": first, "second": second, "tz": db.get_setting("timezone"),
        "favs": db.one("SELECT COUNT(*) n FROM favorite_channels")["n"],
        "devices": [r["name"] for r in db.query("SELECT name FROM tv_devices ORDER BY id")],
        "session_kept": auth.session_user(token) is not None,
        "password_is_the_code": auth.verify("reviewer", os.environ["DEMO_CODE"]) is not None,
        "bad": bad, "was_resetting": any(during), "now_resetting": demo.RESETTING.is_set(),
        "seed_moved": int(db.get_setting(demo.SEED_KEY)) >= seed,
        "no_junk_settings": db.get_setting("report_consent"),
    }))
"""


@pytest.mark.parametrize("stub", [True, pytest.param(False, marks=pytest.mark.skipif(
    not HAVE_FFMPEG, reason="makes the demo's viewing copies with ffmpeg"))])
def test_reset_puts_the_content_back_and_keeps_who_is_in(tmp_path, stub):
    if stub:
        os.environ["STUB_MEDIA"] = "1"
    try:
        out = _run(RESET_SCRIPT, tmp_path)
    finally:
        os.environ.pop("STUB_MEDIA", None)
    got = json.loads(out)
    first, second = got["first"], got["second"]
    assert first == second
    assert first["finished"] == 6 and first["passes"] == 1 and first["scheduled"] >= 3
    assert first["breaks"] == 5 and first["airings"] and first["logos"] == 8
    assert first["users"] == ["reviewer"] and first["viewable"] and first["resume"] >= 2
    assert got["tz"] == "America/Los_Angeles" and got["favs"] == 0
    # The reviewer who was let in is still in; a code nobody approved is gone.
    assert got["devices"] == ["kept"] and got["session_kept"] and got["password_is_the_code"]
    # The sign-in settings were never missing, not for a moment.
    assert got["bad"] == [] and got["now_resetting"] is False
    assert got["seed_moved"]


def test_the_nightly_reset_waits_for_the_next_four_oclock(tmp_path):
    out = _run("""
        import sys
        from datetime import datetime
        sys.argv = ["demo_server.py"]
        sys.path.insert(0, "scripts")
        import demo_server as s
        print(s.seconds_until_reset(datetime(2026, 11, 26, 3, 0, 0)),
              s.seconds_until_reset(datetime(2026, 11, 26, 4, 0, 0)),
              s.seconds_until_reset(datetime(2026, 11, 26, 23, 59, 0)))
    """, tmp_path)
    assert [float(x) for x in out.split()] == [3600.0, 86400.0, 14400.0 + 60.0]


def test_the_demo_refuses_to_start_without_a_code(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DEMO_CODE"}
    env["DEMO_DATA"] = str(tmp_path)
    done = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "demo_server.py")],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "DEMO_CODE" in done.stderr
    env["DEMO_CODE"] = "abcdefghjkm0"
    done = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "demo_server.py")],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "lowercase letters and digits" in done.stderr


# ---------- nothing opens without the code ----------

# What may answer an unsigned visitor, and why: the sign-in routes themselves,
# the page the app's pairing needs (it hands out nothing until the review code
# approves it), and a bare health check. Anything else is a hole.
OPEN_UNSIGNED = {
    ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"),
    ("GET", "/demo"), ("POST", "/demo/pair"),
    ("GET", "/healthz"), ("HEAD", "/healthz"),
    ("GET", "/api/tv/hello"), ("POST", "/api/tv/pair"), ("POST", "/api/tv/pair/poll"),
    ("POST", "/api/tv/pair/code"),
}


def _fill(path):
    import re
    return re.sub(r"\{([^}:]+)(:[^}]*)?\}",
                  lambda m: "1" if "id" in m.group(1) or m.group(1) in ("uid", "pin") else "a" * 32
                  if m.group(1) in ("uid", "token") else "1.1" if m.group(1) == "vcn" else "x",
                  path)


def _walk(make):
    """Every route of the app, asked by a visitor with no real credential. Each
    must say no (401, 403) or send them to sign in, bar the short allowlist."""
    seen = 0
    paths = [(r.path, getattr(r, "methods", None)) for r in app.routes]
    paths = [(p, m) for p, m in paths if m] + [("/static/x.css", {"GET"}), ("/static/", {"GET"})]
    for path, ms in paths:
        for m in sorted(ms | {"HEAD", "OPTIONS"}):
            if (m, path) in OPEN_UNSIGNED:
                continue
            res = make().request(m, _fill(path), data={"x": "1"} if m not in ("GET", "HEAD", "OPTIONS") else None)
            seen += 1
            where = res.headers.get("location", "")
            if path.startswith("/static/"):
                assert res.status_code in (404, 200, 405, 307, 308), (m, path, res.status_code)
                continue
            if m == "OPTIONS" and res.status_code in (200, 204, 405):
                continue
            ok = res.status_code in (401, 403, 405) or (
                res.status_code in (303, 307, 308) and where in ("/login", "/demo"))
            assert ok, (m, path, res.status_code, where)
    return seen


def test_every_route_refuses_a_visitor_without_the_code(on):
    assert _walk(visitor) > 400


def test_every_route_refuses_a_junk_authorization_header(on):
    def junk():
        c = visitor()
        c.headers["Authorization"] = "Bearer not-a-real-key-at-all"
        return c
    assert _walk(junk) > 400


def test_every_route_refuses_a_junk_session_cookie(on):
    def junk():
        c = visitor()
        c.cookies.set(auth.SESSION_COOKIE, "a-session-nobody-made")
        return c
    assert _walk(junk) > 400


def test_every_route_still_refuses_after_the_settings_are_wiped(on):
    """What the nightly reset used to do for a moment: no settings at all must
    not read as "no sign-in"."""
    with db.tx() as c:
        c.execute("DELETE FROM settings")
    auth._con().execute("DELETE FROM users")
    auth._con().commit()
    assert auth.mode() == "local" and exposure.away_on()
    def junk():
        c = visitor()
        c.headers["Authorization"] = "Bearer not-a-real-key-at-all"
        c.cookies.set(auth.SESSION_COOKIE, "nothing")
        return c
    assert _walk(junk) > 400
    # Asking to pair is not approved, and nothing is shown with the key it hands out.
    c = visitor()
    got = c.post("/api/tv/pair", data={"name": "x"}).json()
    polled = c.post("/api/tv/pair/poll", data={"secret": got["secret"]}).json()
    assert polled["approved"] is False and "key" not in polled
    assert db.one("SELECT COUNT(*) n FROM tv_devices WHERE approved_at IS NOT NULL")["n"] == 0
    assert c.get("/api/tv/home", headers={"Authorization": "Bearer junk"}).status_code in (401, 303)


def test_a_request_during_a_reset_gets_503_except_the_health_check(on):
    demo.RESETTING.set()
    try:
        c = visitor()
        assert c.get("/healthz").status_code == 200
        for path in ("/api/tv/hello", "/api/tv/home", "/login", "/demo", "/"):
            assert c.get(path).status_code == 503, path
        assert c.post("/api/tv/pair/code", data={"code": CODE}).status_code == 503
    finally:
        demo.RESETTING.clear()


def test_what_stays_open_shows_nothing(on):
    c = visitor()
    assert c.get("/healthz").json() == {"ok": True}
    assert "recent" not in c.get("/api/tv/hello").json()
    assert "airings" not in c.get("/healthz").text
    for path in ("/", "/guide", "/recordings", "/passes", "/live", "/home", "/search"):
        assert c.get(path).status_code == 303, path
    # Static files for the sign-in page are the one open directory.
    assert c.get("/static/nope-not-there").status_code in (404, 200)


def test_a_wrong_web_password_is_limited_too(on):
    c = visitor()
    seen = [c.post("/login", data={"username": "reviewer", "password": f"wrong-{i}"}
                   ).status_code for i in range(8)]
    assert seen[0] == 303 and seen[-1] == 429


# ---------- the access code, typed in the app ----------

def test_hello_advertises_code_signin_only_in_the_demo(on, monkeypatch):
    assert visitor().get("/api/tv/hello").json()["code_signin"] is True
    monkeypatch.setenv("COUCHELEPHANT_DEMO", "0")
    assert "code_signin" not in visitor().get("/api/tv/hello").json()


def test_the_right_code_makes_a_paired_device_and_its_key_works(on):
    c = visitor()
    got = c.post("/api/tv/pair/code", data={"code": CODE.upper(), "name": "Apple TV"})
    assert got.status_code == 200, got.text
    key = got.json()["key"]
    c.headers["Authorization"] = "Bearer " + key
    assert c.get("/api/tv/hello").json()["paired"] is True
    assert c.get("/api/tv/recordings").status_code == 200
    assert db.one("SELECT COUNT(*) n FROM tv_devices WHERE approved_at IS NOT NULL")["n"] == 1


def test_a_wrong_or_missing_code_makes_nothing(on):
    c = visitor()
    for bad in ("", "nope", CODE + "x"):
        r = c.post("/api/tv/pair/code", data={"code": bad})
        assert r.status_code == 403 and "key" not in r.json()
    assert db.one("SELECT COUNT(*) n FROM tv_devices")["n"] == 0


def test_an_unset_review_code_never_matches(on, monkeypatch):
    monkeypatch.setenv("DEMO_CODE", "")
    assert visitor().post("/api/tv/pair/code", data={"code": ""}).status_code == 403


def test_the_device_list_does_not_grow_without_end(on, monkeypatch):
    monkeypatch.setattr(demo, "MAX_DEVICES", 3)
    for _ in range(6):
        ratelimit.reset()
        assert visitor().post("/api/tv/pair/code", data={"code": CODE}).status_code == 200
    assert db.one("SELECT COUNT(*) n FROM tv_devices")["n"] == 3


def test_a_normal_install_has_no_code_route(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_DEMO", raising=False)
    db.set_setting("auth_mode", "local")
    r = visitor(via_proxy=False, addr="192.168.1.5").post("/api/tv/pair/code",
                                                          data={"code": CODE})
    assert r.status_code in (404, 401, 303) and "key" not in r.text
    assert db.one("SELECT COUNT(*) n FROM tv_devices")["n"] == 0
    monkeypatch.setenv("DEMO_CODE", CODE)
    db.set_setting("auth_mode", "none")
    r = visitor(via_proxy=False, addr="192.168.1.5").post("/api/tv/pair/code",
                                                          data={"code": CODE})
    assert r.status_code == 404
    assert "code_signin" not in visitor(via_proxy=False, addr="192.168.1.5").get(
        "/api/tv/hello").json()


@pytest.mark.parametrize("code,ok", [
    ("k7m3xq9ntw4h2p", True), ("abcdefghjkmn", True), ("abcdefghjkmnpqrs", True),
    ("abcdefghjkm", False), ("abcdefghjkmnpqrst", False), ("abcdefghjkm0", False),
    ("abcdefghjkmo", False), ("abcdefghjkm1", False), ("abcdefghjkml", False),
    ("abcdefghjkmi", False), ("ABCDEFGHJKMN", False), ("abcdefghjkm-", False),
])
def test_the_demo_only_starts_with_a_code_a_remote_can_type(code, ok, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COUCHELEPHANT_")}
    env.update(DEMO_CODE=code, DEMO_DATA=str(tmp_path), PORT="1")
    # Valid codes go on to start; stop them at once by making the port unusable
    # is not needed: only the check is asked about, by importing the module.
    done = subprocess.run([sys.executable, "-c",
                           "import sys; sys.argv=['x']; sys.path.insert(0,'scripts'); "
                           "import demo_server"], env={**env, "PYTHONPATH": ROOT}, cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert (done.returncode == 0) is ok, done.stderr[-300:]


# ---------- what an app's screens may read ----------

def test_an_apps_settings_screen_does_not_get_the_servers_addresses(on):
    db.set_setting("plex_url", "http://10.9.8.7:32400")
    db.set_setting("tuner_url", "http://10.9.8.8")
    got = paired().get("/api/tv/settings")
    assert got.status_code == 200
    text = got.text
    assert "10.9.8" not in text and "plex_url" not in text and "tuner_url" not in text


def test_the_report_reads_are_minimal_and_never_ask_plex_tv(on, monkeypatch):
    from app import plextv
    monkeypatch.setattr(plextv, "account_email",
                        lambda t: (_ for _ in ()).throw(AssertionError("asked plex.tv")))
    c = paired()
    assert c.get("/api/report/email").json() == {"ok": True, "email": ""}
    server = c.get("/api/report/server").json()["server"]
    assert server == {"version": server["version"], "demo": True}


def test_the_settings_pages_send_a_visitor_to_a_short_page(on):
    c = visitor()
    r = c.get("/settings")
    assert r.status_code == 303 and r.headers["location"] == "/demo"
    assert "Settings" in c.get("/demo").text or "demo" in c.get("/demo").text.lower()


def test_the_session_cookie_is_secure_in_the_demo(on):
    c = visitor()
    r = c.post("/login", data={"username": demo.REVIEW_USER, "password": CODE})
    assert "secure" in r.headers["set-cookie"].lower()


# ---------- what a visitor may fill the disk with ----------

def _add(kind, state, uid, started=None, stops=None, created=None, begins=None):
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO recordings (uid, kind, state, title, channel_vcn, begins_at, ends_at, "
            "starts_at, stops_at, started_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, kind, state, uid, "9.1", begins or now + 600, (stops or now + 4000),
             begins or now + 600, stops or now + 4000, started, created or now))
        return cur.lastrowid


def test_a_visitor_has_at_most_two_bookings_and_the_seeded_ones_stay(on):
    from app import recorder
    seeded = [_add("recording", "scheduled", f"seed{i}") for i in range(3)]
    db.set_setting(demo.SEED_KEY, max(seeded))
    mine = [_add("recording", "scheduled", f"mine{i}", begins=int(time.time()) + 600 * (i + 1))
            for i in range(4)]
    demo.enforce_limits()
    left = {r["id"] for r in db.query("SELECT id FROM recordings")}
    assert set(seeded) <= left
    assert left - set(seeded) == set(mine[:2])        # the earliest two stay
    assert recorder.get(mine[3]) is None


def test_a_recording_a_visitor_makes_stops_after_thirty_minutes(on):
    seed = _add("recording", "finished", "seed")
    db.set_setting(demo.SEED_KEY, seed)
    now = int(time.time())
    long = _add("recording", "scheduled", "long", begins=now + 100, stops=now + 100 + 4 * 3600)
    seeded_long = db.one("SELECT stops_at FROM recordings WHERE id = ?", (seed,))["stops_at"]
    demo.enforce_limits()
    assert db.one("SELECT stops_at s FROM recordings WHERE id = ?", (long,))["s"] == \
        now + 100 + demo.MAX_RECORDING_SECONDS
    assert db.one("SELECT stops_at FROM recordings WHERE id = ?", (seed,))["stops_at"] == seeded_long


def test_a_live_session_ends_after_half_an_hour_however_it_is_kept_alive(on):
    now = int(time.time())
    old = _add("live", "recording", "old", created=now - demo.MAX_LIVE_SECONDS - 5)
    fresh = _add("live", "recording", "fresh", created=now - 60)
    demo.enforce_limits()
    ids = {r["id"] for r in db.query("SELECT id FROM recordings")}
    assert old not in ids and fresh in ids


def test_the_recordings_folder_has_a_ceiling_and_only_visitor_recordings_go(on, monkeypatch):
    from app import recorder
    seed = _add("recording", "finished", "seed" * 8)
    db.set_setting(demo.SEED_KEY, seed)
    mine = [_add("recording", "finished", f"{i:032x}") for i in range(3)]
    for r in db.query("SELECT uid FROM recordings"):
        os.makedirs(recorder.rec_dir(r["uid"]), exist_ok=True)
        with open(os.path.join(recorder.rec_dir(r["uid"]), "x.ts"), "wb") as f:
            f.write(b"0" * 1000)
    monkeypatch.setattr(demo, "MAX_DISK_BYTES", 2500)
    monkeypatch.setattr(demo, "_disk_checked", [0.0])
    demo.enforce_limits()
    left = {r["id"] for r in db.query("SELECT id FROM recordings")}
    assert seed in left                       # never
    assert mine[0] not in left and mine[-1] in left    # the oldest of theirs first
    assert _folder_size(recorder.REC_DIR) <= 2500


def _folder_size(path):
    return demo._folder_bytes(path)


def test_a_busy_demo_is_not_reset(on):
    assert demo.recently_used() is False
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO tv_devices (name, key_hash, approved_at, created_at, last_seen) "
                  "VALUES ('x', 'k', ?, ?, ?)", (now - 7200, now - 7200, now - 600))
    assert demo.recently_used() is True
    with db.tx() as c:
        c.execute("UPDATE tv_devices SET last_seen = ?, approved_at = ?", (now - 7200, now - 7200))
    assert demo.recently_used() is False
