"""Use away from home: the switch, the gate, the sign-in limits, the headers."""

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app import auth, db, exposure, ratelimit
from app.routes import tv as tv_routes
from app.web import app

OUT = "8.8.8.8"
HOME = "192.168.1.5"


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    monkeypatch.setattr(exposure, "_adapters", lambda: [])
    monkeypatch.setattr(exposure, "_route_files", lambda: [])
    exposure.clear_cache()
    ratelimit.reset()
    yield
    exposure.clear_cache()
    ratelimit.reset()


def at(addr, scheme="http"):
    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (addr, 4321)}
        await app(scope, receive, send)
    c = TestClient(shim, base_url=f"{scheme}://testserver", follow_redirects=False)
    c.headers.update({"Origin": f"{scheme}://testserver", "Host": "testserver"})
    return c


def _local_with_account(name="owner", pw="a-good-password"):
    db.set_setting("auth_mode", "local")
    return auth.create_user(name, pw)


def _signed_in(c, uid):
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
    return c


def _away(on=True):
    db.set_setting("away_mode", "1" if on else "0")


# ---------- the gate ----------

def test_it_is_off_by_default():
    assert exposure.away_on() is False
    c = at(OUT)
    _local_with_account()
    assert c.get("/settings").status_code == 303   # the old gate, unchanged


def test_an_outsider_is_sent_to_sign_in_and_cannot_see_the_rest():
    _local_with_account()
    _away()
    c = at(OUT)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    for p in ("/settings", "/settings/auth", "/partial/settings", "/welcome", "/setup",
              "/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code in (401, 404), p
    assert c.get("/api/tv/hello").status_code == 200
    assert c.get("/static/css/app.css").status_code == 200
    assert c.get("/favicon.ico").status_code != 303
    assert c.get("/api/tv/home").status_code == 401


def test_even_a_signed_in_outsider_never_reaches_settings():
    uid = _local_with_account()
    _away()
    c = _signed_in(at(OUT), uid)
    for p in ("/settings", "/partial/settings", "/setup", "/welcome", "/docs",
              "/openapi.json", "/redoc"):
        assert c.get(p).status_code in (401, 404), p
    assert c.post("/settings/auth", data={"auth_mode": "none"}).status_code in (401, 404)
    assert c.get("/api/away/check").status_code in (401, 404)
    assert c.get("/guide").status_code == 200


def test_the_same_requests_from_home_behave_as_today():
    uid = _local_with_account()
    _away()
    c = at(HOME)
    assert c.get("/").status_code == 303
    assert c.get("/docs").status_code in (303, 404)
    _signed_in(c, uid)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/settings").status_code == 200


def test_the_gate_runs_before_the_sign_in_gate():
    names = [m.kwargs.get("dispatch", getattr(m, "cls", None)).__name__
             for m in app.user_middleware if "dispatch" in m.kwargs]
    assert names.index("_away_gate") < names.index("_auth_gate")
    assert names[0] == "_headers"        # the outermost, so every answer gets them
    # And in behaviour: with sign-in off the old gate lets everything by, and
    # the away gate must still hold the line first.
    db.set_setting("auth_mode", "none")
    _away()
    assert at(OUT).get("/settings").status_code in (401, 404)


def test_a_device_key_reaches_what_a_device_may_and_no_more():
    uid = _local_with_account()
    home = at(HOME)
    key = _paired_signed(home, uid)
    _away()
    c = at(OUT)
    assert c.get("/api/tv/home", headers=key).status_code == 200
    assert c.get("/api/tv/devices", headers=key).status_code in (401, 404)
    assert c.get("/settings", headers=key).status_code in (401, 404)
    assert c.get("/api/tv/home", headers={"Authorization": "Bearer nope"}).status_code == 401


def _paired_signed(c, uid):
    asked = c.post("/api/tv/pair", data={"name": "Den"}).json()
    _signed_in(c, uid)
    c.post("/api/tv/devices/approve", data={"code": asked["code"]})
    c.cookies.clear()
    key = c.post("/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]
    return {"Authorization": f"Bearer {key}"}


def test_a_passed_video_link_streams_and_a_bad_pass_does_not():
    uid = _local_with_account()
    _paired_signed(at(HOME), uid)
    dev = db.one("SELECT * FROM tv_devices")
    rec = "0" * 32
    good = tv_routes.stream_pass(dev, rec)
    home = at(HOME).get(f"/hlsk/{good}/{rec}/master.m3u8")
    assert home.status_code not in (303, 401)
    out = at(OUT).get(f"/hlsk/{good}/{rec}/master.m3u8")
    assert out.status_code == home.status_code
    bad = at(OUT).get(f"/hlsk/{good[:-1]}{'1' if good[-1] == '0' else '0'}/{rec}/master.m3u8")
    assert bad.status_code in (303, 401)
    assert at(OUT).get(f"/hls/{rec}/master.m3u8").status_code in (303, 401, 404)


def test_a_request_from_the_routers_public_address_must_sign_in():
    _local_with_account()
    _away()
    c = at("203.0.113.7")                   # what a hairpinned home device looks like
    assert c.get("/").headers["location"] == "/login"


# ---------- the switch ----------

def test_the_switch_refuses_while_sign_in_is_off():
    c = at(HOME)
    r = c.post("/settings/away", data={"on": "1"}).json()
    assert r["ok"] is False and r["fix"] == "make_account" and r["error"]
    assert exposure.away_on() is False


def test_the_switch_refuses_while_an_account_has_no_password():
    db.set_setting("auth_mode", "local")
    auth._con().execute("INSERT INTO users (username, pw_hash, pw_salt, role, created_at) "
                        "VALUES ('kid', '', '', 'user', 1)")
    auth._con().commit()
    admin = auth.create_user("owner", "a-good-password")
    c = _signed_in(at(HOME), admin)
    r = c.post("/settings/away", data={"on": "1"}).json()
    assert r["ok"] is False and r["fix"] == "set_passwords"
    assert [u["username"] for u in r["users"]] == ["kid"]
    uid = auth.list_users()[0]["id"]
    assert c.post("/settings/away/password",
                  data={"uid": uid, "password": "kid-password-1"}).json()["ok"] is True
    assert c.post("/settings/away", data={"on": "1"}).json() == {"ok": True}


def test_the_switch_refuses_with_no_account_in_local_mode():
    from app.routes import settings
    db.set_setting("auth_mode", "local")
    assert settings._away_blocker()["fix"] == "make_account"


def test_the_first_account_can_be_made_from_the_switch():
    c = at(HOME)
    r = c.post("/settings/away/account", data={"username": "me", "password": "short"}).json()
    assert r["ok"] is False
    r = c.post("/settings/away/account",
               data={"username": "me", "password": "a-good-password"})
    assert r.json()["ok"] is True
    assert auth.mode() == "local"
    assert c.cookies.get(auth.SESSION_COOKIE), "the person who made it is signed in"
    assert c.post("/settings/away", data={"on": "1"}).json()["ok"] is True
    assert exposure.away_on() is True


def test_cloudflare_mode_turns_on_with_a_local_account():
    uid = _local_with_account()
    c = _signed_in(at(HOME), uid)
    db.set_setting("auth_mode", "cloudflare")
    assert c.post("/settings/away", data={"on": "1"}).json() == {"ok": True}


def test_the_session_that_turned_it_on_keeps_working():
    uid = _local_with_account()
    c = _signed_in(at(OUT), uid)            # the owner, through a VPN that looks outside
    assert c.post("/settings/away", data={"on": "1"}).json() == {"ok": True}
    assert exposure.away_on()
    assert c.get("/guide").status_code == 200
    assert c.get("/").headers.get("location") != "/login"


def test_turning_it_off_from_home_works_and_from_outside_does_not():
    uid = _local_with_account()
    _away()
    assert _signed_in(at(OUT), uid).post("/settings/away", data={"on": "0"}).status_code in (401, 404)
    assert exposure.away_on()
    assert _signed_in(at(HOME), uid).post("/settings/away", data={"on": "0"}).json() == {"ok": True}
    assert not exposure.away_on()


def test_sign_in_cannot_be_weakened_while_away_is_on():
    uid = _local_with_account()
    _away()
    c = _signed_in(at(HOME), uid)
    r = c.post("/settings/auth", data={"auth_mode": "none"})
    assert "Turn off Use away from home first" in urllib.parse.unquote(r.headers["location"])
    assert auth.mode() == "local" and exposure.away_on()
    # A user with no password is the same hole.
    auth._con().execute("INSERT INTO users (username, pw_hash, pw_salt, role, created_at) "
                        "VALUES ('kid', '', '', 'user', 1)")
    auth._con().commit()
    db.set_setting("auth_mode", "cloudflare")
    r = c.post("/settings/auth", data={"auth_mode": "local"})
    assert "Turn off Use away from home first" in urllib.parse.unquote(r.headers["location"])
    assert auth.mode() == "cloudflare"
    # And the last account cannot be removed.
    db.set_setting("auth_mode", "local")
    auth._con().execute("DELETE FROM users WHERE username='kid'")
    auth._con().commit()
    other = auth.create_user("other", "another-password")
    c2 = _signed_in(at(HOME), other)
    c2.post(f"/settings/users/{uid}/delete")
    assert auth.user_count() == 1


def test_the_self_check_warns_about_a_cloudflare_tunnel_with_no_trusted_proxy():
    uid = _local_with_account()
    c = _signed_in(at(HOME), uid)
    db.set_setting("auth_mode", "cloudflare")
    item = next(i for i in c.get("/api/away/check").json()["items"] if i["name"] == "Settings")
    assert item["ok"] is False
    assert item["say"] == ("A Cloudflare tunnel on this network makes every visitor look "
                           "home. Add the tunnel's address under Trusted proxies, just above.")
    db.set_setting("trusted_proxies", "172.18.0.2")
    item = next(i for i in c.get("/api/away/check").json()["items"] if i["name"] == "Settings")
    assert item["ok"] is True and "paired TV" in item["say"]


def test_the_self_check_lists_safe_and_unsafe():
    uid = _local_with_account()
    d = _signed_in(at(HOME), uid).get("/api/away/check").json()
    says = [i["say"] for i in d["items"]]
    assert all(set(i) == {"name", "ok", "say"} for i in d["items"])
    assert any(not i["ok"] for i in d["items"]) and any(i["ok"] for i in d["items"])
    assert ("Your router may send home devices through the internet address; "
            "they will be asked to sign in.") in says


# ---------- sign-in limits ----------

def _wrong(c, name="owner"):
    return c.post("/login", data={"username": name, "password": "wrong-wrong"})


def test_the_sixth_wrong_password_is_refused_for_a_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: now[0])
    _local_with_account()
    c = at(OUT)
    for _ in range(5):
        assert _wrong(c).status_code == 303
    r = _wrong(c)
    assert r.status_code == 429 and "Too many tries. Wait a minute." in r.text
    # Even the right password waits.
    assert c.post("/login", data={"username": "owner",
                                  "password": "a-good-password"}).status_code == 429
    now[0] += 61
    assert c.post("/login", data={"username": "owner",
                                  "password": "a-good-password"}).status_code == 303


def test_the_next_lockout_doubles_up_to_an_hour(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: now[0])
    for want in (60, 120, 240, 480, 960, 1920, 3600, 3600):
        for _ in range(5):
            ratelimit.fail("k")
        assert round(ratelimit.check("k")) == want
        now[0] += want + 1


def test_failures_age_out_after_ten_minutes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: now[0])
    for _ in range(4):
        ratelimit.fail("k")
    now[0] += 601
    ratelimit.fail("k")
    assert ratelimit.check("k") == 0


def test_one_account_is_limited_across_many_addresses():
    _local_with_account()
    for i in range(5):
        _wrong(at(f"8.8.8.{i + 1}"))
    assert _wrong(at("9.9.9.9")).status_code == 429


def test_one_address_is_limited_across_many_accounts():
    _local_with_account()
    c = at(OUT)
    for i in range(5):
        _wrong(c, f"nobody{i}")
    assert _wrong(c, "owner").status_code == 429
    assert _wrong(at("9.9.9.9"), "owner").status_code == 303


def test_a_good_sign_in_clears_the_account_count():
    _local_with_account()
    for _ in range(4):
        _wrong(at("8.8.8.8"))
    assert at("8.8.4.4").post("/login", data={"username": "owner",
                                              "password": "a-good-password"}).status_code == 303
    for _ in range(4):
        assert _wrong(at("9.9.9.9")).status_code == 303


def test_a_good_sign_in_gives_its_own_try_back_to_the_address():
    _local_with_account()
    c = at(OUT)
    for _ in range(20):
        assert c.post("/login", data={"username": "owner",
                                      "password": "a-good-password"}).status_code == 303


def test_a_failure_is_logged_with_the_address_and_never_the_password(caplog):
    _local_with_account()
    with caplog.at_level("WARNING"):
        _wrong(at(OUT))
    text = caplog.text
    assert OUT in text and "wrong-wrong" not in text


def test_pairing_is_closed_to_outsiders_while_away_is_on():
    _local_with_account()
    _away()
    out = at(OUT)
    for p in ("/api/tv/pair", "/api/tv/pair/poll"):
        r = out.post(p, data={"name": "x", "secret": "y"})
        assert r.status_code == 403
        assert r.json()["error"] == "Pair this device at home first."
    assert at(HOME).post("/api/tv/pair", data={"name": "x"}).status_code == 200


def test_pairing_opens_when_the_one_small_function_says_so(monkeypatch):
    import app.web as web
    _local_with_account()
    _away()
    monkeypatch.setattr(web, "pairing_open_to_outsiders", lambda request: True)
    out = at(OUT)
    codes = [out.post("/api/tv/pair", data={"name": "x"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


# ---------- the Host header cannot move the gates ----------

HOSTILE = ["x/login?", "a/static/?", "a/api/tv/hello?", "a/login#", "x@y", "a b"]


@pytest.mark.parametrize("host", HOSTILE)
def test_a_hostile_host_cannot_reach_settings_from_outside(host):
    uid = _local_with_account("owner")
    _away()
    _local_signed = _signed_in(at(HOME), uid)
    out = at(OUT)
    out.headers["Host"] = host
    out.headers["Origin"] = f"http://{host}"
    for r in (out.get("/settings"), out.post("/settings/away", data={"on": "0"}),
              out.post("/settings/auth", data={"auth_mode": "none"})):
        assert r.status_code in (400, 401, 403, 404, 303) and r.status_code != 200
    assert exposure.away_on() and auth.mode() == "local"


@pytest.mark.parametrize("host", HOSTILE)
def test_a_hostile_host_cannot_bypass_local_sign_in_with_away_off(host):
    _local_with_account()
    c = at(OUT)
    c.headers["Host"] = host
    for r in (c.get("/settings"), c.get("/partial/settings"),
              c.post("/settings/auth", data={"auth_mode": "none"})):
        assert r.status_code != 200
    assert auth.mode() == "local"


def test_a_host_that_is_not_a_host_is_a_bad_request():
    c = at(HOME)
    c.headers["Host"] = "x/login?"
    r = c.get("/healthz")
    assert r.status_code == 400
    assert "content-security-policy" in r.headers


def test_the_gates_read_the_path_the_router_uses():
    import inspect

    import app.web as web
    for fn in (web._away_gate, web._auth_gate, web._first_run):
        assert "request.url.path" not in inspect.getsource(fn)


def test_the_stream_path_check_uses_the_real_path():
    # A Host that fakes a pass address must not make a request look passed.
    uid = _local_with_account()
    _paired_signed(at(HOME), uid)
    dev = db.one("SELECT * FROM tv_devices")
    rec = "0" * 32
    good = tv_routes.stream_pass(dev, rec)
    c = at(OUT)
    c.headers["Host"] = f"x/hlsk/{good}/{rec}/m.m3u8?"
    assert c.get("/settings").status_code != 200


# ---------- Origin: null ----------

@pytest.mark.parametrize("origin", ["null", "not a url", "://", ""])
def test_a_post_with_a_null_or_unparseable_origin_is_refused(origin):
    c = at(HOME)
    c.headers["Origin"] = origin
    if origin == "":
        del c.headers["Origin"]
        c.headers["Referer"] = "null"
    r = c.post("/settings/away/account", data={"username": "me", "password": "a-good-password"})
    assert r.status_code == 403
    assert auth.mode() == "none"


# ---------- limits, round two ----------

def test_the_account_lock_does_not_apply_to_home():
    _local_with_account()
    for _ in range(5):
        _wrong(at(OUT))                    # outside: the account is now locked
    assert _wrong(at("9.9.9.9")).status_code == 429
    home = at(HOME)
    r = home.post("/login", data={"username": "owner", "password": "a-good-password"})
    assert r.status_code == 303 and "/login" not in r.headers["location"]


def test_home_failures_do_not_lock_the_account_for_the_owner_outside():
    _local_with_account()
    for i in range(8):
        _wrong(at(f"192.168.1.{i + 10}"))
    r = at(OUT).post("/login", data={"username": "owner", "password": "a-good-password"})
    assert r.status_code == 303 and "/login" not in r.headers["location"]


def test_an_ipv6_address_is_keyed_by_its_64():
    assert ratelimit.addr_key("2001:db8:1:2:aaaa::1") == ratelimit.addr_key("2001:db8:1:2:bbbb::9")
    assert ratelimit.addr_key("2001:db8:1:3::1") != ratelimit.addr_key("2001:db8:1:2::1")
    assert ratelimit.addr_key("8.8.8.8") != ratelimit.addr_key("8.8.8.9")
    _local_with_account()
    for i in range(5):
        _wrong(at(f"2001:db8:1:2::{i + 1}"), f"n{i}")
    assert _wrong(at("2001:db8:1:2:ffff::1"), "other").status_code == 429


def test_parallel_attempts_cannot_all_pass_the_check():
    import threading
    admitted = []

    def go():
        admitted.append(ratelimit.reserve(["addr:t", "user:t"]) == 0)
    threads = [threading.Thread(target=go) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(admitted) == 5


def test_a_right_answer_gives_its_try_back():
    for _ in range(4):
        assert ratelimit.reserve(["k"]) == 0
    assert ratelimit.reserve(["k"]) == 0
    ratelimit.refund("k")
    assert ratelimit.reserve(["k"]) == 0
    assert ratelimit.reserve(["k"]) > 0


def test_the_table_is_capped_and_drops_the_oldest(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 50)
    for i in range(200):
        ratelimit.fail(f"k{i}")
    assert len(ratelimit._keys) == 50
    assert "k199" in ratelimit._keys and "k0" not in ratelimit._keys


def test_an_unknown_name_is_not_written_to_the_log(caplog):
    _local_with_account()
    with caplog.at_level("WARNING"):
        _wrong(at(OUT), "hunter2-typed-in-the-name-box")
        _wrong(at(OUT), "owner")
    assert "hunter2-typed-in-the-name-box" not in caplog.text
    assert "unknown account" in caplog.text and "'owner'" not in caplog.text and "owner" in caplog.text


# ---------- headers ----------

def test_every_answer_carries_the_security_headers():
    c = at(HOME)
    for r in (c.get("/healthz"), c.get("/login"), c.get("/nope"),
              at(OUT).get("/settings")):
        h = r.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "SAMEORIGIN"
        assert h["referrer-policy"] == "same-origin"
        assert "default-src 'self'" in h["content-security-policy"]
        assert "frame-ancestors 'self'" in h["content-security-policy"]
        assert "server" not in h
        assert "strict-transport-security" not in h


def test_hsts_only_on_https_answers():
    r = at(HOME, "https").get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    assert "strict-transport-security" not in at(HOME).get("/healthz").headers


def test_a_server_error_carries_the_headers_too():
    from fastapi import APIRouter
    r = APIRouter()

    @r.get("/boom-for-test")
    def boom():
        raise RuntimeError("no")
    app.include_router(r)
    try:
        async def shim(scope, receive, send):
            await app(scope, receive, send)
        c = TestClient(shim, base_url="https://testserver", raise_server_exceptions=False)
        resp = c.get("/boom-for-test")
    finally:
        app.router.routes[:] = [x for x in app.router.routes
                                if getattr(x, "path", "") != "/boom-for-test"]
    assert resp.status_code == 500
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in resp.headers
    assert resp.headers["strict-transport-security"] == "max-age=31536000"
    assert "server" not in resp.headers


def test_a_flood_of_made_up_names_cannot_push_out_a_locked_account(monkeypatch):
    from app import ratelimit
    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 50)
    for _ in range(ratelimit.FAILS):
        ratelimit.reserve(["addr:203.0.113.9", "user:owner"])
    assert ratelimit.reserve(["addr:203.0.113.9", "user:owner"]) > 0
    # The locked address floods made-up names: nothing is added for them.
    for i in range(500):
        assert ratelimit.reserve(["addr:203.0.113.9", f"user:junk{i}"]) > 0
    assert len(ratelimit._keys) <= 50
    # Unlocked addresses fill the table past the cap; the lock stays.
    for i in range(200):
        ratelimit.reserve([f"addr:198.51.100.{i % 250}", f"user:x{i}"])
    assert len(ratelimit._keys) <= 50
    assert ratelimit.reserve(["addr:192.0.2.1", "user:owner"]) > 0
    ratelimit.reset()


# ---------- the final review round ----------

@pytest.mark.parametrize("method,path", [
    ("post", "/api/import"), ("post", "/api/import/inspect"), ("get", "/api/export"),
    ("post", "/api/backups/restore"), ("get", "/api/backups/jobs"),
    ("post", "/api/backingstore/run"), ("get", "/api/backingstore/config"),
    ("post", "/api/update/apply"),
    ("get", "/api/tv/devices"), ("post", "/api/tv/devices/approve"),
    ("post", "/api/tv/devices/1/delete"), ("get", "/api/connect.svg"),
    ("get", "/api/recorder/library-suggest")])
def test_admin_apis_are_not_found_from_outside_even_signed_in(method, path):
    uid = _local_with_account()
    _away()
    c = _signed_in(at(OUT), uid)
    assert getattr(c, method)(path).status_code == 404


def test_a_signed_in_person_outside_cannot_change_tv_settings_but_a_device_can():
    uid = _local_with_account()
    key = _paired_signed(at(HOME), uid)
    _away()
    person = _signed_in(at(OUT), uid)
    assert person.post("/api/tv/settings", data={"dry_run": "1"}).status_code == 404
    assert person.get("/api/tv/settings").status_code == 404
    assert at(OUT).get("/api/tv/settings", headers=key).status_code == 200


def test_no_host_header_from_outside_is_refused_while_away_is_on():
    _local_with_account()
    _away()
    c = at(OUT)
    assert c.get("/login", headers={"Host": ""}).status_code == 400
    assert c.get("/login").status_code == 200
    _away(False)
    assert c.get("/login", headers={"Host": ""}).status_code == 200


def test_a_flood_of_locked_keys_cannot_grow_the_table_past_a_ceiling(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 50)
    for i in range(400):
        for _ in range(ratelimit.FAILS):
            ratelimit.fail(f"k{i}")          # every one of them ends up locked
    assert len(ratelimit._keys) <= 50 * 6 // 5
    assert "k399" in ratelimit._keys and "k0" not in ratelimit._keys


def test_the_proxies_route_saves_and_refuses():
    c = at(HOME)
    bad = c.post("/settings/away/proxies", data={"proxies": "::/0"}).json()
    assert bad["ok"] is False and "wide" in bad["error"].lower()
    assert not db.get_setting("trusted_proxies")
    ok = c.post("/settings/away/proxies", data={"proxies": "192.0.2.10"}).json()
    assert ok["ok"] is True and db.get_setting("trusted_proxies") == "192.0.2.10"


def test_the_self_check_warns_about_a_proxy_nobody_trusted():
    c = _signed_in(at(HOME), _local_with_account())
    plain = c.get("/api/away/check").json()["items"]
    assert not any("Proxy" == i["name"] for i in plain)
    exposure.is_home(type("R", (), {
        "client": type("C", (), {"host": "127.0.0.1"})(),
        "headers": __import__("starlette.datastructures", fromlist=["Headers"]).Headers(
            raw=[(b"x-forwarded-for", b"8.8.8.8")])})())
    items = c.get("/api/away/check").json()["items"]
    warn = next(i for i in items if i["name"] == "Proxy")
    assert warn["ok"] is False and "Trusted proxies" in warn["say"]


# ---------- the issue report reads ----------

REPORT = ("/api/report/server", "/api/report/email")


def _paired_key(c):
    asked = c.post("/api/tv/pair", data={"name": "Den"}).json()
    return {"Authorization": "Bearer " + c.post(
        "/api/tv/pair/poll", data={"secret": asked["secret"]}).json()["key"]}


def test_report_reads_from_away():
    home = at(HOME)
    key = _paired_key(home)
    admin = _local_with_account()
    kid = auth.create_user("kid", "a-good-password", role="user")
    _away()
    for path in REPORT:
        out = at(OUT)
        assert out.get(path, headers=key).status_code == 200
        assert out.get(path).status_code == 404
        _signed_in(out, admin)
        assert out.get(path).status_code == 404          # a session is not enough away
        k = _signed_in(at(OUT), kid)
        assert k.get(path).status_code == 404
        # at home the sign-in rules are unchanged
        h = at(HOME)
        assert h.get(path).status_code == 401
        assert _signed_in(at(HOME), admin).get(path).status_code == 200
        assert _signed_in(at(HOME), kid).get(path).status_code == 403
        assert at(HOME).get(path, headers=key).status_code == 200


def test_report_send_and_consent_for_signed_in_person_from_away():
    # The away gate lets any signed-in session through to the normal rules.
    _local_with_account()
    kid = auth.create_user("kid", "a-good-password", role="user")
    _away()
    assert at(OUT).get("/api/report/consent").status_code == 401
    k = _signed_in(at(OUT), kid)
    assert k.get("/api/report/consent").json()["ok"] is True
    assert k.post("/api/report/consent", data={"agree": "1"}).json() == {"ok": True}
    r = k.post("/api/report/send", json={"trigger": {"kind": "support"}, "message": "x",
                                         "email": ""})
    assert r.status_code != 403 and r.status_code != 404


# ---------- the session cookie's Secure flag ----------

def _cookie_header(c):
    r = c.post("/login", data={"username": "owner", "password": "a-good-password"})
    assert r.status_code == 303, r.text
    return r.headers["set-cookie"].lower()


def test_the_session_cookie_is_secure_over_https_and_not_over_http():
    _local_with_account()
    assert "secure" in _cookie_header(at(HOME, scheme="https"))
    assert "secure" not in _cookie_header(at(HOME, scheme="http"))


def test_a_spoofed_forwarded_proto_from_an_untrusted_peer_changes_nothing():
    _local_with_account()
    c = at(HOME)
    c.headers["X-Forwarded-Proto"] = "https"
    assert "secure" not in _cookie_header(c)


def test_forwarded_proto_counts_only_from_a_trusted_proxy():
    _local_with_account()
    db.set_setting("trusted_proxies", "192.168.1.5")
    c = at(HOME)
    c.headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-For": "8.8.8.8"})
    assert "secure" in _cookie_header(c)
    c = at(HOME)
    c.headers.update({"X-Forwarded-Proto": "http", "X-Forwarded-For": "8.8.8.8"})
    assert "secure" not in _cookie_header(c)
