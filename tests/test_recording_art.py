"""A recording keeps what it needs to be shown. The guide is a source for that
and never the only copy, because the guide forgets a programme once it has aired."""
import time

from app import db, recart, recorder
from tests.test_recorder import _airing

ART = "https://metadata-static.plex.tv/a/art.jpg"
THUMB = "https://metadata-static.plex.tv/a/thumb.jpg"
GUIDE_TABLES = ("airings", "programs", "plex_grabs", "plex_subscriptions")


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

def test_no_art_anywhere_gets_a_title_card_that_needs_no_sign_in(client):
    _finished("u1", guid=None)
    art = client.get("/api/tv/home").json()["recent"][0]["art"]
    assert "/card/" in art
    path = art.split("testserver", 1)[1]
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_card_address_cannot_be_guessed(client):
    _finished("u1", guid=None)
    assert client.get("/card/u1/" + "0" * 32 + ".png").status_code == 404
    assert client.get("/card/nobody/" + "0" * 32 + ".png").status_code == 404


def test_the_card_route_is_open_to_a_television_with_no_key():
    from app import web
    assert web._is_open("/card/abc/def.png")


# ---------- the whole class: the guide is empty and nothing blanks ----------

def test_every_recording_api_survives_an_empty_guide(client, synced):
    _program("plex://episode/abc")
    _finished("u1")
    _finished("u2", guid=None, title="Local Thing", show_title=None)
    _finished("u3", guid="plex://episode/zzz", state="finished")
    _finished("u4", guid="plex://episode/yyy", state="recording", begins_at=1, ends_at=2)
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, title, parent_title, channel_vcn, begins_at, ends_at)"
                  " VALUES ('g9', 'Ep', 'Show', '5.1', 1, ?)", (int(time.time()) + 3600,))
    aid = _airing("a-x", "g-x", "5.1", int(time.time()) + 7200)
    recorder.book(aid)
    recart.refresh_from_guide()
    with db.tx() as c:
        for t in GUIDE_TABLES:
            c.execute(f"DELETE FROM {t}")
    seen = []
    home = client.get("/api/tv/home").json()
    seen += home["recent"] + home["live_now"]
    seen += client.get("/api/tv/recordings").json()["recorded"]
    seen.append(client.get(f"/api/tv/recordings/{seen[0]['id']}").json()["recording"])
    assert len(seen) >= 8
    for r in seen:
        assert r["title"], r
        assert r["art"], r
    sched = client.get("/api/tv/schedule").json()["rows"]
    assert all(row["title"] for row in sched if str(row["id"]).startswith("ce:"))
    assert client.get("/recorded").status_code in (200, 303, 404)


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
    assert not [n for n in db.query("SELECT * FROM notices WHERE resolved_at IS NULL")
                if n["code"] == "recording_art"]


def test_the_daily_check_raises_a_notice_only_when_repair_fails(monkeypatch):
    _finished("u1", title=None, show_title=None, guid=None, plex_rating_key=None)
    recart.daily_check(None, now=10_000)
    codes = [n["code"] for n in db.query("SELECT code FROM notices WHERE resolved_at IS NULL")]
    assert "recording_art" in codes


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


def test_the_web_pages_and_the_failed_bookings_survive_an_empty_guide(client, synced):
    begins = int(time.time()) + 7200
    _finished("u1")
    with db.tx() as c:
        c.execute("INSERT INTO pass_actions (pass_id, program_guid, airing_id, program_title,"
                  " parent_title, channel_vcn, begins_at, action, reason, dry_run, created_at)"
                  " VALUES (1, 'gone', 'a1', 'Ep', 'Show', '5.1', ?, 'failed', 'nope', 0, 1)",
                  (begins,))
        for t in GUIDE_TABLES:
            c.execute(f"DELETE FROM {t}")
    for path in ("/home", "/recordings"):
        assert client.get(path).status_code == 200
    rows = client.get("/api/schedule").json()["rows"]
    assert rows and all(r["title"] for r in rows)
    assert [r for r in rows if r["status"] == "failed"][0]["parent"] == "Show"
    for r in client.get("/api/tv/recordings").json()["recorded"]:
        assert r["title"] and r["art"] and r["channel"]["name"]
