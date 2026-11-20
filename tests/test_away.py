"""Use away from home: the switch, the gate, the sign-in limits, the headers."""

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
    bad = at(OUT).get(f"/hlsk/{good[:-1]}0/{rec}/master.m3u8")
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


def test_sign_in_cannot_be_turned_off_while_away_is_on():
    uid = _local_with_account()
    _away()
    c = _signed_in(at(HOME), uid)
    c.post("/settings/auth", data={"auth_mode": "none"})
    assert not exposure.away_on()


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


def test_a_good_sign_in_clears_the_count():
    _local_with_account()
    c = at(OUT)
    for _ in range(4):
        _wrong(c)
    assert c.post("/login", data={"username": "owner",
                                  "password": "a-good-password"}).status_code == 303
    for _ in range(4):
        assert _wrong(c).status_code == 303


def test_a_failure_is_logged_with_the_address_and_never_the_password(caplog):
    _local_with_account()
    with caplog.at_level("WARNING"):
        _wrong(at(OUT))
    text = caplog.text
    assert OUT in text and "wrong-wrong" not in text


def test_pairing_is_counted_from_outside_only():
    _local_with_account()
    _away()
    out = at(OUT)
    codes = [out.post("/api/tv/pair", data={"name": "x"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    home = at(HOME)
    assert all(home.post("/api/tv/pair", data={"name": "x"}).status_code == 200
               for _ in range(7))


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
