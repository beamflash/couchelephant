"""Demo mode: the public copy for the App Store reviewer (app/demo.py)."""
import json
import os
import shutil
import subprocess
import sys
import textwrap

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
    yield
    exposure.clear_cache()
    ratelimit.reset()


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
    c = TestClient(shim, base_url="http://testserver", follow_redirects=False)
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
                   ).status_code for i in range(7)]
    # The pairing ask above already counted once from this address.
    assert seen[0] == 403 and seen[-1] == 429 and seen.count(403) >= 3
    # Even the right one waits out the lock.
    assert c.post("/demo/pair", data={"review_code": CODE, "code": got["code"]}
                  ).status_code == 429


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
    assert signed_in(visitor()).get(path).status_code in (403, 404)
    assert paired().get(path).status_code in (403, 404)


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
    stranger = visitor(via_proxy=False)
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
    env.update(PYTHONPATH=ROOT, DEMO_CODE=CODE, DEMO_DATA=str(tmp_path / "data"),
               DEMO_MEDIA=str(tmp_path / "media"), DEMO_MEDIA_SCALE="0.02")
    if shutil.which("ffmpeg"):
        env["COUCHELEPHANT_FFMPEG"] = shutil.which("ffmpeg")
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip().splitlines()[-1]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="makes the demo's viewing copies with ffmpeg")
def test_reset_puts_the_seeded_state_back(tmp_path):
    out = _run("""
        import json, os, sys
        sys.argv = ["demo_server.py"]
        sys.path.insert(0, "scripts")
        import demo_server as s
        from tests import demo_world
        from app import auth, db, recorder
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
                "devices": n("SELECT COUNT(*) n FROM tv_devices"),
                "users": [u["username"] for u in auth.list_users()],
                "resume": db.one("SELECT COUNT(*) n FROM recordings WHERE position > 0")["n"],
                "viewable": all(os.path.exists(recorder.playlist_path(r["uid"]))
                                for r in db.query("SELECT uid FROM recordings WHERE state='finished'")),
            }

        demo_world.reset(conf, s.MEDIA, "reviewer", os.environ["DEMO_CODE"])
        first = look()
        # A visitor makes a mess.
        with db.tx() as c:
            c.execute("DELETE FROM recordings WHERE state='finished'")
            c.execute("DELETE FROM passes")
            c.execute("INSERT INTO tv_devices (name, approved_at, created_at) VALUES ('x', 1, 1)")
            c.execute("INSERT INTO favorite_channels (vcn) VALUES ('9.1')")
        db.set_setting("timezone", "UTC")
        auth.create_user("intruder", "an-intruder-password")
        demo_world.reset(conf, s.MEDIA, "reviewer", os.environ["DEMO_CODE"])
        second = look()
        print(json.dumps([first, second, db.get_setting("timezone"),
                          db.one("SELECT COUNT(*) n FROM favorite_channels")["n"]]))
    """, tmp_path)
    first, second, tz, favs = json.loads(out)
    assert first == second
    assert first["finished"] == 6 and first["passes"] == 1 and first["scheduled"] >= 3
    assert first["breaks"] == 5 and first["airings"] and first["logos"] == 8
    assert first["users"] == ["reviewer"] and first["devices"] == 0 and first["viewable"]
    assert first["resume"] >= 2
    assert tz == "America/Los_Angeles" and favs == 0


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
    env["DEMO_CODE"] = "elevenchars"
    done = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "demo_server.py")],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "12 characters" in done.stderr


# ---------- nothing opens without the code ----------

# What may answer an unsigned visitor, and why: the sign-in routes themselves,
# the page the app's pairing needs (it hands out nothing until the review code
# approves it), and a bare health check. Anything else is a hole.
OPEN_UNSIGNED = {
    ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"),
    ("GET", "/demo"), ("POST", "/demo/pair"),
    ("GET", "/healthz"),
    ("GET", "/api/tv/hello"), ("POST", "/api/tv/pair"), ("POST", "/api/tv/pair/poll"),
}


def _fill(path):
    import re
    return re.sub(r"\{([^}:]+)(:[^}]*)?\}",
                  lambda m: "1" if "id" in m.group(1) or m.group(1) in ("uid", "pin") else "a" * 32
                  if m.group(1) in ("uid", "token") else "1.1" if m.group(1) == "vcn" else "x",
                  path)


def test_every_route_refuses_a_visitor_without_the_code(on):
    seen = 0
    for r in app.routes:
        methods = getattr(r, "methods", None)
        if not methods:
            continue
        for m in sorted(methods - {"HEAD"}):
            if (m, r.path) in OPEN_UNSIGNED:
                continue
            c = visitor()
            res = c.request(m, _fill(r.path), data={"x": "1"} if m != "GET" else None)
            seen += 1
            assert res.status_code in (401, 403, 303, 307, 308), (m, r.path, res.status_code)
            if res.status_code in (303, 307, 308):
                assert "/login" in res.headers["location"], (m, r.path, res.headers["location"])
    assert seen > 150


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
