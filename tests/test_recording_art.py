"""A recording keeps what it needs to be shown. The guide is a source for that
and never the only copy, because the guide forgets a programme once it has aired."""
import time

import pytest

from app import auth, db, recart, recorder
from tests.test_recorder import _airing

ART = "https://metadata-static.plex.tv/a/art.jpg"
THUMB = "https://metadata-static.plex.tv/a/thumb.jpg"
GUIDE_TABLES = ("airings", "programs", "plex_grabs", "plex_subscriptions", "channels", "teams")


def _finished(uid, guid="plex://episode/abc", state="in_plex", **kw):
    cols = dict(uid=uid, kind="recording", state=state, program_guid=guid,
                title="An Egg to Die For", show_title="Murder, She Wrote",
                channel_vcn="5.1", channel_title="5.1 KCTV", begins_at=1000,
                ends_at=4600, plex_rating_key="234038", created_at=1, updated_at=1)
    cols.update(kw)
    with db.tx() as c:
        cur = c.execute(f"INSERT INTO recordings ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
    return cur.lastrowid


def _program(guid, art=ART, thumb=THUMB):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, grandparent_title, art, thumb,"
                  " summary, rating_key) VALUES (?,?,?,?,?,?,?)",
                  (guid, "An Egg to Die For", "Murder, She Wrote", art, thumb, "Jessica.", "rk"))


def _rec(rec_id):
    return db.one("SELECT * FROM recordings WHERE id = ?", (rec_id,))


# ---------- kept on the row ----------

def test_booking_copies_the_art_onto_the_recording(synced):
    aid = _airing("a-art", "g-art", "5.1", int(time.time()) + 3600)
    with db.tx() as c:
        c.execute("UPDATE programs SET art = ?, thumb = ? WHERE guid = 'g-art'", (ART, THUMB))
    rec = _rec(recorder.book(aid))
    assert rec["art"] == ART and rec["thumb"] == THUMB


def test_art_survives_the_guide_forgetting_the_programme(client):
    _program("plex://episode/abc")
    _finished("u1")
    recart.refresh_from_guide()
    with db.tx() as c:
        c.execute("DELETE FROM programs")
    d = client.get("/api/tv/home").json()
    assert [r["art"] for r in d["recent"]] == [ART]


def test_the_copy_is_refreshed_while_the_guide_still_has_it(client):
    _program("plex://episode/abc", art=ART)
    rid = _finished("u1")
    recart.refresh_from_guide()
    _program("plex://episode/abc", art="https://metadata-static.plex.tv/new.jpg")
    recart.refresh_from_guide()
    assert _rec(rid)["art"].endswith("new.jpg")


def test_a_guide_row_without_art_does_not_blank_the_kept_one():
    _program("plex://episode/abc")
    rid = _finished("u1")
    recart.refresh_from_guide()
    _program("plex://episode/abc", art="", thumb="")
    recart.refresh_from_guide()
    assert _rec(rid)["art"] == ART


def test_the_one_time_copy_fills_rows_the_guide_still_matches():
    _program("plex://episode/abc")
    rid = _finished("u1")
    assert not _rec(rid)["art"]
    conn = db.connect()
    recart.migrate(conn)
    conn.commit()
    assert _rec(rid)["art"] == ART and _rec(rid)["summary"] == "Jessica."


# ---------- fetched again when the guide is gone ----------

def test_backfill_asks_plex_for_a_recording_the_guide_forgot(monkeypatch):
    rid = _finished("u1", guid="plex://episode/5fc70024bcd67c002d0de395")
    asked = []

    def fake(guid, token):
        asked.append(guid)
        return {"art": ART, "thumb": THUMB}
    monkeypatch.setattr(recart, "discover", fake)
    db.set_setting("plex_token", "secret-token")
    recart.backfill(None, now=10_000)
    assert asked == ["plex://episode/5fc70024bcd67c002d0de395"]
    assert _rec(rid)["art"] == ART


def test_a_failed_fetch_is_remembered_and_not_retried_at_once(monkeypatch):
    rid = _finished("u1")
    calls = []

    def boom(guid, token):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(recart, "discover", boom)
    db.set_setting("plex_token", "t")
    recart.backfill(None, now=10_000)
    recart.backfill(None, now=10_060)
    assert len(calls) == 1 and _rec(rid)["art_fails"] == 1
    recart.backfill(None, now=10_000 + 2 * 3600)
    assert len(calls) == 2


def test_an_address_that_carries_a_token_is_never_kept(monkeypatch):
    rid = _finished("u1")
    monkeypatch.setattr(recart, "discover", lambda g, t: {
        "art": "https://x.example/a.jpg?X-Plex-Token=abc", "thumb": "/library/metadata/1/thumb/2"})
    db.set_setting("plex_token", "t")
    recart.backfill(None, now=10_000)
    assert not _rec(rid)["art"] and not _rec(rid)["thumb"]


# ---------- never a blank tile ----------

def _path(url):
    assert url.startswith("http://testserver"), url
    return url[len("http://testserver"):]


def test_no_art_anywhere_gets_a_title_card_from_the_authenticated_route(client):
    _finished("u1", guid=None)
    art = client.get("/api/tv/home").json()["recent"][0]["art"]
    assert art == "http://testserver/api/tv/art/u1"
    r = client.get(_path(art))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_picture_route_needs_the_same_sign_in_as_the_rest(client):
    _finished("u1", guid=None)
    db.set_setting("auth_mode", "local")
    auth.create_user("owner", "a-good-password")
    assert client.get("/api/tv/art/u1").status_code == 401
    assert recart.art_url("x", "u1") == "x/api/tv/art/u1"


def test_there_is_no_open_card_route_any_more(client):
    from app import web
    assert not web._is_open("/card/abc/def.png")
    assert client.get("/card/u1/" + "0" * 32 + ".png").status_code in (401, 404)


def test_a_picture_for_nobody_is_a_404(client):
    assert client.get("/api/tv/art/nobody").status_code == 404


def test_the_library_picture_is_fetched_here_with_the_token_in_a_header(client, monkeypatch):
    from app.routes import art as art_route
    seen = []

    class R:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return {"MediaContainer": {"Metadata": [{"thumb": "/library/metadata/1/thumb/2"}]}}

        def iter_bytes(self):
            yield b"JPEGDATA"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_get(url, **kw):
        seen.append((url, kw.get("headers")))
        return R(b"")

    def fake_stream(method, url, **kw):
        seen.append((url, kw.get("headers")))
        return R(b"")
    monkeypatch.setattr(art_route.httpx, "get", fake_get)
    monkeypatch.setattr(art_route.httpx, "stream", fake_stream)
    db.set_setting("plex_url", "http://plex.test:32400")
    db.set_setting("plex_token", "SECRET")
    art_route._reset()
    _finished("u1", guid=None)
    r = client.get("/api/tv/art/u1")
    assert r.status_code == 200 and r.content == b"JPEGDATA"
    assert all(h and h["X-Plex-Token"] == "SECRET" for _, h in seen)
    assert all("SECRET" not in url for url, _ in seen)
    assert "SECRET" not in r.headers.get("location", "")


def test_a_failed_library_picture_falls_back_to_the_card_and_is_not_retried(client, monkeypatch):
    from app.routes import art as art_route
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(art_route.httpx, "get", boom)
    db.set_setting("plex_url", "http://plex.test:32400")
    db.set_setting("plex_token", "t")
    art_route._reset()
    _finished("u1", guid=None)
    for _ in range(3):
        assert client.get("/api/tv/art/u1").headers["content-type"] == "image/png"
    assert len(calls) == 1


# ---------- the whole class: the guide is empty and nothing blanks ----------

def test_every_recording_api_survives_an_empty_guide(client, synced):
    _program("plex://episode/abc")
    with db.tx() as c:
        c.execute("UPDATE programs SET content_rating = 'TV-PG' WHERE guid = 'plex://episode/abc'")
    first = _finished("u1")
    _finished("u2", guid=None, title="Local Thing", show_title=None)
    _finished("u3", guid="plex://episode/zzz", state="finished")
    _finished("u4", guid="plex://episode/yyy", state="recording", begins_at=1, ends_at=2)
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, title, parent_title, channel_vcn, begins_at, ends_at)"
                  " VALUES ('g9', 'Ep', 'Show', '5.1', 1, ?)", (int(time.time()) + 3600,))
    aid = _airing("a-x", "g-x", "5.1", int(time.time()) + 7200)
    recorder.book(aid)
    with db.tx() as c:
        c.execute("INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn, begins_at,"
                  " source, pass_id, pass_uid, created_at) VALUES ('ag', 'plex://episode/abc', 'Ep',"
                  " '5.1', ?, 'pass', 98, 'gone-uid', 1)", (int(time.time()) + 7200,))
    recart.refresh_from_guide()
    with db.tx() as c:
        for t in GUIDE_TABLES:
            c.execute(f"DELETE FROM {t}")
    from app import passes
    assert [o["title"] for o in passes.orphans() if o["lost_id"] == 98] == ["Murder, She Wrote"]

    # Each surface, with the guide gone.
    home = client.get("/api/tv/home").json()
    lists = {"home recent": home["recent"], "home live": home["live_now"],
             "recordings": client.get("/api/tv/recordings").json()["recorded"],
             "detail": [client.get(f"/api/tv/recordings/{first}").json()["recording"]]}
    for name, rows in lists.items():
        assert rows, name
        for r in rows:
            assert r["title"], (name, r)
            assert r["art"], (name, r)
            assert r["channel"]["name"], (name, r)
    # The KEPT picture is what is used where there is one, not only the card.
    assert ART in [r["art"] for r in home["recent"]]
    assert lists["recordings"][0]["art"] == ART or ART in [r["art"] for r in lists["recordings"]]
    assert lists["detail"][0]["art"] == ART
    # What is on screen at that point keeps its rating.
    now = client.get(f"/api/tv/recordings/{first}/now").json()["program"]
    assert now["title"] and now["rating"] == "TV-PG"
    # Every picture address answers with an image, or sends to a public one.
    for rows in lists.values():
        for r in rows:
            got = client.get(_path(r["art"]), follow_redirects=False) \
                if r["art"].startswith("http://testserver") else None
            if got is not None:
                assert got.status_code in (200, 302)
                assert got.status_code == 302 or got.headers["content-type"].startswith("image/")
    sched = client.get("/api/tv/schedule").json()["rows"]
    assert all(row["title"] for row in sched if str(row["id"]).startswith("ce:"))


# ---------- the daily self-check ----------

def test_the_daily_check_repairs_a_missing_title_from_plex(monkeypatch):
    rid = _finished("u1", title=None, show_title=None, guid=None)

    class FakePlex:
        def metadata(self, provider, key):
            assert provider == "library" and key == "234038"
            return {"title": "An Egg to Die For", "grandparentTitle": "Murder, She Wrote",
                    "summary": "Jessica.", "parentIndex": 11, "index": 11, "year": 1995}
    monkeypatch.setattr(recart, "discover", lambda g, t: {})
    recart.daily_check(FakePlex(), now=10_000)
    r = _rec(rid)
    assert r["title"] == "An Egg to Die For" and r["season"] == 11
    assert "recording_art" not in _open_codes()


def _open_codes():
    return [n["code"] for n in db.query("SELECT code FROM notices WHERE resolved_at IS NULL")]


def test_the_daily_check_raises_a_notice_only_when_a_title_cannot_be_repaired():
    _finished("u1", title=None, show_title=None, guid=None, plex_rating_key=None)
    recart.daily_check(None, now=10_000)
    assert "recording_art" in _open_codes()


def test_a_missing_picture_alone_is_not_a_fault(monkeypatch):
    _finished("u1", guid="plex://episode/x")
    monkeypatch.setattr(recart, "discover", lambda g, t: {})
    db.set_setting("plex_token", "t")
    for day in range(6):
        recart.daily_check(None, now=10_000 + day * 8 * 86400)
    assert "recording_art" not in _open_codes()


def test_the_daily_sweep_asks_about_a_few_recordings_a_pass_and_respects_backoff(monkeypatch):
    for i in range(50):
        _finished(f"u{i}", guid=f"plex://episode/{i}")
    asked = []

    def miss(g, t):
        asked.append(g)
        return {}
    monkeypatch.setattr(recart, "discover", miss)
    db.set_setting("plex_token", "t")
    recart.daily_check(None, now=10_000)
    assert len(asked) == recart.DAILY_CAP
    recart.daily_check(None, now=10_060)             # inside the wait: only the untried ones
    assert len(asked) == 2 * recart.DAILY_CAP


def test_a_pass_stops_starting_fetches_when_its_time_is_used(monkeypatch):
    for i in range(5):
        _finished(f"u{i}", guid=f"plex://episode/{i}")
    asked = []
    monkeypatch.setattr(recart, "discover", lambda g, t: asked.append(g) or {})
    db.set_setting("plex_token", "t")
    recart.backfill(None, now=10_000, limit=5, deadline=time.monotonic() - 1)
    assert asked == []


def test_a_hung_fetch_does_not_hold_up_overtime(monkeypatch):
    import threading

    from app import overtime
    release = threading.Event()
    started = threading.Event()

    def hang():
        started.set()
        release.wait(30)
    monkeypatch.setattr(recart, "tick", hang)
    recart._stop.clear()
    t0 = time.monotonic()
    monkeypatch.setattr(recart._stop, "wait", lambda s=None: False if s == 20 else release.wait(30))
    recart.start()
    assert started.wait(5)
    overtime.tick()
    assert time.monotonic() - t0 < 5
    release.set()
    recart.stop()
    assert recart._thread.daemon


def test_the_art_work_is_not_in_the_overtime_loop():
    import inspect

    from app import web
    assert "recart" not in inspect.getsource(web.outside_loop)


def test_a_non_https_discovery_address_is_refused(monkeypatch):
    import importlib
    monkeypatch.setenv("COUCHELEPHANT_DISCOVER_URL", "http://evil.example")
    mod = importlib.reload(recart)
    try:
        assert mod.DISCOVER_URL.startswith("https://discover.provider.plex.tv")
    finally:
        monkeypatch.delenv("COUCHELEPHANT_DISCOVER_URL")
        importlib.reload(recart)


def test_an_oversized_stored_address_is_dropped():
    assert recart._public("https://x.example/" + "a" * 2000) == ""


def test_what_the_guide_offers_goes_through_the_same_check_when_copied():
    _program("plex://episode/abc", art="https://x.example/a.jpg?X-Plex-Token=abc", thumb=THUMB)
    rid = _finished("u1")
    recart.refresh_from_guide()
    assert not _rec(rid)["art"] and _rec(rid)["thumb"] == THUMB


# ---------- the rest of what outlives the guide ----------

TEAMS = '[{"id": 1, "name": "Chiefs"}, {"id": 2, "name": "Broncos"}]'


def test_the_teams_are_read_from_the_recording_first():
    from app import overtime
    rid = _finished("u1", guid="plex://episode/gone", title="Game", teams=TEAMS)
    assert overtime.guide_teams(_rec(rid)) == ["Chiefs", "Broncos"]


def test_a_pass_sees_the_teams_of_its_own_recording_after_the_guide_forgot():
    from app import passes
    _finished("u1", guid="plex://episode/gone", state="scheduled", teams=TEAMS,
              source="pass", pass_id=7, begins_at=int(time.time()) + 3600,
              starts_at=int(time.time()) + 3000, airing_id="a1")
    got = passes._pass_bookings(7)
    assert got and got[0]["teams"] == TEAMS


def test_a_booking_log_keeps_the_show_name():
    from app import passes
    passes._log(3, {"program_guid": "g", "id": "a", "title": "Ep", "grandparent_title": "Show",
                    "channel_vcn": "5.1", "begins_at": 5}, "failed", "no", False)
    assert db.one("SELECT parent_title FROM pass_actions")["parent_title"] == "Show"


def test_the_one_time_copy_fills_the_show_name_of_old_log_lines():
    _program("g1")
    with db.tx() as c:
        c.execute("INSERT INTO pass_actions (program_guid, program_title, action) "
                  "VALUES ('g1', 'Ep', 'failed')")
    conn = db.connect()
    recart.migrate(conn)
    conn.commit()
    assert db.one("SELECT parent_title FROM pass_actions")["parent_title"] == "Murder, She Wrote"


def test_a_failed_booking_keeps_its_show_name_when_the_guide_is_empty():
    from app.routes import passes as pass_routes
    begins = int(time.time()) + 7200
    with db.tx() as c:
        c.execute("INSERT INTO pass_actions (pass_id, program_guid, airing_id, program_title,"
                  " parent_title, channel_vcn, begins_at, action, reason, dry_run, created_at)"
                  " VALUES (1, 'gone', 'a1', 'Ep', 'Show', '5.1', ?, 'failed', 'nope', 0, 1)",
                  (begins,))
    rows = pass_routes._schedule_rows()
    failed = [r for r in rows if r["status"] == "failed"]
    assert failed and failed[0]["title"] == "Ep" and failed[0]["parent"] == "Show"


def test_a_channel_that_left_the_lineup_still_has_a_name(client):
    from starlette.requests import Request

    from app.routes import tv
    req = Request({"type": "http", "headers": [(b"host", b"testserver")], "scheme": "http",
                   "server": ("testserver", 80), "path": "/", "query_string": b""})
    _finished("u1", channel_vcn="77.1", channel_title="77.1 WXYZ")
    assert tv._channel(req, "77.1", "")["name"] == "77.1 WXYZ"
    assert tv._channel(req, "88.8", "")["name"] == "Channel 88.8"


def test_sport_is_told_from_the_recordings_when_the_guide_forgot():
    from app.routes import tv
    _finished("u1", show_title="NFL Football", section="sports", guid=None)
    assert tv._sport_show("NFL Football")


def test_a_recording_never_has_an_empty_title(client):
    _finished("u1", title=None, show_title=None, guid=None)
    assert client.get("/api/tv/home").json()["recent"][0]["title"]


def test_a_destination_that_heard_every_fault_hears_about_art_too():
    from app import health, notify
    others = [c for c, _ in notify.FAULTS if c != health.RECORDING_ART]
    with db.tx() as c:
        c.execute("DELETE FROM destinations")
        c.execute("DELETE FROM settings WHERE key = 'mig_art_event'")
        c.execute("INSERT INTO destinations (name, kind, events) VALUES ('d', 'webhook', ?)",
                  (",".join(others),))
    notify.add_art_event()
    got = db.one("SELECT events FROM destinations")["events"].split(",")
    assert health.RECORDING_ART in got
    assert health.RECORDING_ART in [c for c, _ in notify.FAULTS]


def test_the_web_pages_draw_what_the_api_gives_after_an_empty_guide(client, synced):
    begins = int(time.time()) + 7200
    _program("plex://episode/abc")
    _finished("u1")
    _finished("u2", guid=None)
    recart.refresh_from_guide()
    with db.tx() as c:
        c.execute("INSERT INTO pass_actions (pass_id, program_guid, airing_id, program_title,"
                  " parent_title, channel_vcn, begins_at, action, reason, dry_run, created_at)"
                  " VALUES (1, 'gone', 'a1', 'Ep', 'Show', '5.1', ?, 'failed', 'nope', 0, 1)",
                  (begins,))
        for t in GUIDE_TABLES:
            c.execute(f"DELETE FROM {t}")
    # The Home page is built in the browser from /api/tv/home, using r.art as
    # the card picture and r.title as its name; the recordings page the same
    # from /api/schedule.
    home = client.get("/home")
    assert home.status_code == 200
    assert "/api/tv/home" in home.text and "r.art" in home.text and "esc(r.title)" in home.text
    page = client.get("/recordings")
    assert page.status_code == 200 and "/api/schedule" in page.text
    cards = client.get("/api/tv/home").json()["recent"]
    assert len(cards) == 2 and all(c["title"] and c["art"] for c in cards)
    assert any(c["art"] == ART for c in cards)
    rows = client.get("/api/schedule").json()["rows"]
    assert rows and all(r["title"] for r in rows)
    assert [r for r in rows if r["status"] == "failed"][0]["parent"] == "Show"


def test_a_pass_sees_the_teams_of_a_plex_booking_after_the_guide_forgot():
    from app import passes
    begins = int(time.time()) + 7200
    with db.tx() as c:
        c.execute("INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn, begins_at,"
                  " source, pass_id, teams, created_at) VALUES ('a1', 'gone', 'Game', '5.1', ?,"
                  " 'pass', 4, ?, 1)", (begins, TEAMS))
    got = passes._pass_bookings(4)
    assert got and got[0]["teams"] == TEAMS


def test_the_one_time_copy_gives_old_plex_bookings_their_teams():
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, teams) VALUES ('g1', 'G', ?)",
                  (TEAMS,))
        c.execute("INSERT INTO our_grabs (airing_id, program_guid, created_at) VALUES ('a', 'g1', 1)")
    conn = db.connect()
    recart.migrate(conn)
    conn.commit()
    assert db.one("SELECT teams FROM our_grabs")["teams"] == TEAMS


def test_a_destination_with_the_recording_art_event_hears_the_notice(monkeypatch):
    from app import health, notify
    from tests import fake_chat
    url = fake_chat.start()
    monkeypatch.setattr(notify, "DISCORD_HOSTS", frozenset({"127.0.0.1", "localhost"}))
    monkeypatch.setattr(notify, "DISCORD_SCHEMES", frozenset({"http"}))
    try:
        notify.save_destination(name="Apps", kind="discord", events=[health.RECORDING_ART],
                                remind_hours=24, webhook=f"{url}/api/webhooks/1/a")
        _finished("u1", title=None, show_title=None, guid=None, plex_rating_key=None)
        now = int(time.time())
        recart.daily_check(None, now=now)
        assert "recording_art" in _open_codes()
        assert notify.dispatch(now=now) == 1
        assert len(fake_chat.discord_sent()) == 1
    finally:
        fake_chat.stop()


def test_a_lost_pass_keeps_its_show_and_sport_when_the_guide_forgot():
    from app import passes
    begins = int(time.time()) + 7200
    _program("g-lost")
    with db.tx() as c:
        c.execute("INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn, begins_at,"
                  " source, pass_id, pass_uid, created_at) VALUES ('a1', 'g-lost', 'Ep', '5.1', ?,"
                  " 'pass', 99, 'gone-uid', 1)", (begins,))
    conn = db.connect()
    recart.migrate(conn)                          # what the guide still knows is copied
    conn.commit()
    with db.tx() as c:
        c.execute("DELETE FROM programs")
    got = [o for o in passes.orphans() if o["lost_id"] == 99]
    assert got and got[0]["title"] == "Murder, She Wrote"


def test_a_booking_made_now_keeps_the_show_and_section():
    from app import passes
    _program("g2")
    with db.tx() as c:
        c.execute("UPDATE programs SET section = 'sports' WHERE guid = 'g2'")
    passes.remember({"id": "a2", "program_guid": "g2", "title": "Ep", "channel_vcn": "5.1",
                     "begins_at": 5}, "pass", "sub", pass_id=1)
    row = db.one("SELECT show_title, section FROM our_grabs WHERE airing_id = 'a2'")
    assert row and row["show_title"] == "Murder, She Wrote" and row["section"] == "sports"


# ---------- the picture route: proxying, caps, no redirect ----------

class _Resp:
    def __init__(self, body=b"IMG", kind="image/jpeg", status=200, length=None):
        self.body, self.status_code = body, status
        self.headers = {} if kind is None else {"content-type": kind}
        if length is not None:
            self.headers["content-length"] = str(length)

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError("status")

    def iter_bytes(self):
        step = 64 * 1024
        for i in range(0, len(self.body), step):
            yield self.body[i:i + step]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def art_route(monkeypatch):
    from app.routes import art as mod
    mod._reset()
    return mod


def _serve(monkeypatch, art_route, resp, calls=None):
    def fake_stream(method, url, **kw):
        if calls is not None:
            calls.append(url)
        return resp() if callable(resp) else resp
    monkeypatch.setattr(art_route.httpx, "stream", fake_stream)


def test_a_kept_public_picture_is_proxied_with_a_200_never_redirected(client, art_route,
                                                                      monkeypatch):
    _finished("u1", art=ART, guid=None)
    calls = []
    _serve(monkeypatch, art_route, _Resp(b"PICTURE", "image/png"), calls)
    r = client.get("/api/tv/art/u1", follow_redirects=False)
    assert r.status_code == 200 and r.content == b"PICTURE"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert calls == [ART]


def test_a_picture_over_the_cap_is_not_served(client, art_route, monkeypatch):
    _finished("u1", art=ART, guid=None)
    _serve(monkeypatch, art_route, _Resp(b"x" * (art_route.MAX_IMAGE + 10)))
    r = client.get("/api/tv/art/u1", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"   # the card


def test_a_declared_length_over_the_cap_is_refused_before_reading(client, art_route, monkeypatch):
    _finished("u1", art=ART, guid=None)
    _serve(monkeypatch, art_route, _Resp(b"x", length=art_route.MAX_IMAGE + 1))
    assert client.get("/api/tv/art/u1").headers["content-type"] == "image/png"


@pytest.mark.parametrize("kind", ["image/svg+xml", "text/html", "application/octet-stream",
                                  "image/gif", None])
def test_only_jpeg_png_and_webp_are_proxied(client, art_route, monkeypatch, kind):
    _finished("u1", art=ART, guid=None)
    _serve(monkeypatch, art_route, _Resp(b"<svg/>", kind))
    r = client.get("/api/tv/art/u1")
    assert r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"


@pytest.mark.parametrize("url", [
    "http://metadata-static.plex.tv/a.jpg", "https://127.0.0.1/a.jpg", "https://10.0.0.5/a.jpg",
    "https://192.168.1.2/a.jpg", "https://169.254.169.254/latest", "https://localhost/a.jpg",
    "https://printer.local/a.jpg", "https://[::1]/a.jpg", "https://2130706433/a.jpg",
    "https://intranet/a.jpg", "https://user@x.example/a.jpg", "ftp://x.example/a.jpg",
])
def test_an_address_that_is_not_a_public_https_one_is_never_kept(url):
    assert recart._public(url) == ""


def test_a_public_https_address_is_kept():
    assert recart._public(ART) == ART


def test_a_private_address_in_a_stored_row_is_never_fetched_or_handed_out(client, art_route,
                                                                          monkeypatch):
    _finished("u1", art="http://10.0.0.5/a.jpg", thumb="https://localhost/b.jpg", guid=None)
    calls = []
    _serve(monkeypatch, art_route, _Resp(), calls)
    r = client.get("/api/tv/art/u1", follow_redirects=False)
    assert calls == [] and r.headers["content-type"] == "image/png"
    assert client.get("/api/tv/home").json()["recent"][0]["art"] == "http://testserver/api/tv/art/u1"


def test_raw_guide_art_is_checked_before_a_client_gets_it(client):
    _finished("u1", guid="plex://episode/abc")
    _program("plex://episode/abc", art="http://10.0.0.5/a.jpg", thumb="")
    art = client.get("/api/tv/home").json()["recent"][0]["art"]
    assert art == "http://testserver/api/tv/art/u1"


def test_the_route_never_redirects(client, art_route, monkeypatch):
    _finished("u1", art=ART, guid=None)
    for resp in (_Resp(b"A", "image/jpeg"), _Resp(b"", "image/jpeg", status=302), _Resp(b"A", None)):
        art_route._reset()
        _serve(monkeypatch, art_route, resp)
        assert client.get("/api/tv/art/u1", follow_redirects=False).status_code == 200


def test_one_upstream_fetch_serves_everyone_asking_at_once(art_route):
    import threading
    gate, calls, out = threading.Event(), [], []

    def fetch():
        calls.append(1)
        gate.wait(5)
        return (b"X", "image/png")
    threads = [threading.Thread(target=lambda: out.append(art_route._get("k", fetch)))
               for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1 and out == [(b"X", "image/png")] * 6


def test_a_slow_upstream_costs_a_waiting_caller_only_a_short_wait(art_route, monkeypatch):
    import threading
    monkeypatch.setattr(art_route, "WAIT", 0.2)
    gate = threading.Event()
    leader = threading.Thread(target=lambda: art_route._get("k", lambda: (gate.wait(5), None)[1]))
    leader.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    assert art_route._get("k", lambda: (b"never", "image/png")) is None
    assert time.monotonic() - t0 < 2
    gate.set()
    leader.join(5)


def test_the_picture_cache_stays_inside_its_byte_budget(art_route, monkeypatch):
    monkeypatch.setattr(art_route, "CACHE_BYTES", 1000)
    for i in range(20):
        art_route._get(f"k{i}", lambda: (b"x" * 300, "image/png"))
    assert art_route._bytes[0] <= 1000
    assert sum(len(v[0]) for v in art_route._cache.values()) == art_route._bytes[0]


def test_old_misses_are_forgotten(art_route):
    art_route._missed.update({f"k{i}": 1.0 for i in range(10)})
    art_route._get("fresh", lambda: None)
    assert "k0" not in art_route._missed and "fresh" in art_route._missed


def test_start_runs_a_fresh_thread_when_the_old_one_died(monkeypatch):
    import threading
    monkeypatch.setattr(recart, "tick", lambda: None)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    recart._thread = dead
    recart._stop.set()
    recart.start(interval=0.05)
    try:
        assert recart._thread is not dead and recart._thread.is_alive()
        assert not recart._stop.is_set()
    finally:
        recart.stop()


def test_a_dripping_download_stops_at_the_deadline(monkeypatch):
    # A server that sends a byte at a time must not hold a fetch slot for long.
    import itertools

    from app.routes import art

    class Slow:
        headers = {"content-type": "image/png"}
        def raise_for_status(self): pass
        def iter_bytes(self):
            for _ in itertools.count():
                yield b"x"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    clock = iter(range(0, 10_000))
    monkeypatch.setattr(art.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(art.httpx, "stream", lambda *a, **k: Slow())
    assert art._download("https://img.example.com/a.png") is None


def test_a_percent_sign_in_the_host_is_not_kept():
    from app import recart
    assert recart._public("https://127.0.0.%31/x.jpg") == ""
    assert recart._public("https://metadata-static.plex.tv/a.jpg")


def test_a_title_card_is_not_cached_for_long():
    from app.routes import art
    assert "max-age=300" in art.CARD_HEADERS["Cache-Control"]
