"""Deleting a pass can cancel its upcoming recordings, and the bookings of a
pass that is already gone can be managed as a group."""
import time

from app import db, passes, recorder
from tests import fake_plex
from tests.test_recorder import _airing


def _series_pass(title="Murder, She Wrote", n=3, section="tv"):
    """A series pass with n bookings of ours ahead of now."""
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, series_title, series_guid, uid, recorder, "
                  "enabled, created_at) VALUES ('series', ?, ?, 'u-msw', 'couchelephant', 1, 0)",
                  (title, title))
    pid = db.one("SELECT id FROM passes")["id"]
    for i in range(n):
        row = _airing(f"s{i}", f"gs{i}", "5.1", now + 7200 + i * 4000,
                      title=f"Episode {i}", show=title)
        with db.tx() as c:
            c.execute("UPDATE programs SET section = ? WHERE guid = ?", (section, f"gs{i}"))
        recorder.book(row, "pass", pass_id=pid, pass_uid="u-msw")
    return pid


def _states():
    return sorted(r["state"] for r in db.query("SELECT state FROM recordings"))


def test_the_pass_list_says_how_many_upcoming_recordings_it_has(client, tuner, plex):
    _series_pass(n=3)
    p = client.get("/api/tv/recordings").json()["passes"][0]
    assert p["upcoming"] == 3
    assert client.get("/api/rules").json()["rules"][0]["upcoming"] == 3


def test_only_what_has_not_started_counts_and_is_cancelled(client, tuner, plex):
    pid = _series_pass(n=4)
    ids = [r["id"] for r in db.query("SELECT id FROM recordings ORDER BY id")]
    with db.tx() as c:
        c.execute("UPDATE recordings SET state = 'recording' WHERE id = ?", (ids[0],))
        c.execute("UPDATE recordings SET state = 'finished' WHERE id = ?", (ids[1],))
    assert client.get("/api/tv/recordings").json()["passes"][0]["upcoming"] == 2
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"}).json()
    assert r["ok"] is True and r["cancelled"] == 2
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 0
    assert _states() == ["finished", "recording"]


def test_deleting_without_the_flag_keeps_the_bookings(client, tuner, plex):
    pid = _series_pass(n=3)
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "0"}).json()
    assert r["ok"] is True and r["cancelled"] == 0
    assert _states() == ["scheduled"] * 3


def test_the_web_delete_takes_the_same_flag(client, tuner, plex):
    pid = _series_pass(n=2)
    r = client.post(f"/passes/{pid}/delete", data={"cancel_upcoming": "1"}).json()
    assert r["cancelled"] == 2 and _states() == []


def test_plex_bookings_of_a_pass_are_cancelled_in_plex(client, synced, tuner):
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    n = len([d for d in passes.run_passes() if d["action"] == "scheduled"])
    assert n and len(fake_plex.STATE.subscriptions) == n
    pid = db.one("SELECT id FROM passes")["id"]
    assert client.get("/api/tv/recordings").json()["passes"][0]["upcoming"] == n
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"}).json()
    assert r["cancelled"] == n
    assert fake_plex.STATE.subscriptions == {}
    assert db.one("SELECT COUNT(*) c FROM our_grabs")["c"] == 0


def test_a_pass_stays_when_plex_cannot_cancel(client, synced, tuner):
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    passes.run_passes()
    pid = db.one("SELECT id FROM passes")["id"]
    db.set_setting("plex_url", "http://127.0.0.1:1")
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"})
    assert r.status_code == 502 and r.json()["ok"] is False
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 1


def _lost(client, **kw):
    pid = _series_pass(**kw)
    client.post(f"/api/tv/passes/{pid}/delete")
    return pid


def test_bookings_of_a_deleted_pass_show_as_one_row_per_lost_pass(client, tuner, plex):
    pid = _lost(client, n=3)
    row = [p for p in client.get("/api/tv/recordings").json()["passes"]
           if p["who"] == "orphan"][0]
    assert row["title"] == "Murder, She Wrote (pass deleted)"
    assert (row["tab"], row["upcoming"], row["booked"], row["can_follow"],
            row["editable"], row["lost_id"]) == ("season", 3, 3, True, False, pid)
    assert client.get("/api/tv/passes/lost").json()["lost"][0]["upcoming"] == 3
    web = [r for r in client.get("/api/rules").json()["rules"] if r["who"] == "orphan"]
    assert len(web) == 1 and web[0]["icon"] == "series"


def test_a_live_pass_is_not_an_orphan(client, tuner, plex):
    _series_pass(n=2)
    assert client.get("/api/tv/passes/lost").json()["lost"] == []


def test_cancel_all_cancels_a_lost_pass_s_upcoming_bookings(client, tuner, plex):
    pid = _lost(client, n=3)
    with db.tx() as c:
        c.execute("UPDATE recordings SET state = 'finished' WHERE id = "
                  "(SELECT MIN(id) FROM recordings)")
    r = client.post(f"/api/tv/passes/lost/{pid}/cancel").json()
    assert r["ok"] is True and r["cancelled"] == 2
    assert _states() == ["finished"]
    assert client.get("/api/tv/passes/lost").json()["lost"] == []


def test_follow_again_makes_the_pass_and_takes_the_bookings_back(client, tuner, plex):
    pid = _lost(client, n=3)
    r = client.post(f"/api/tv/passes/lost/{pid}/follow").json()
    assert r["ok"] is True
    p = db.one("SELECT * FROM passes")
    assert (p["kind"], p["series_title"], p["recorder"]) == \
        ("series", "Murder, She Wrote", "couchelephant")
    assert {(x["pass_id"], x["pass_uid"]) for x in db.query("SELECT * FROM recordings")} \
        == {(p["id"], p["uid"])}
    assert client.get("/api/tv/passes/lost").json()["lost"] == []
    listed = client.get("/api/tv/recordings").json()["passes"]
    assert [(x["who"], x["upcoming"]) for x in listed] == [("ce", 3)]
    # And it is an ordinary pass again.
    assert client.post(f"/api/tv/passes/{p['id']}/delete",
                       data={"cancel_upcoming": "1"}).json()["cancelled"] == 3


def test_a_lost_team_pass_can_be_cancelled_but_not_rebuilt(client, tuner, plex):
    pid = _lost(client, n=2, section="sports")
    row = [p for p in client.get("/api/tv/recordings").json()["passes"]
           if p["who"] == "orphan"][0]
    assert row["tab"] == "sports" and row["can_follow"] is False
    r = client.post(f"/api/tv/passes/lost/{pid}/follow")
    assert r.status_code == 409 and db.one("SELECT COUNT(*) c FROM passes")["c"] == 0
    assert client.post(f"/api/tv/passes/lost/{pid}/cancel").json()["cancelled"] == 2


# ---------- review fixes ----------

def test_a_booking_that_started_meanwhile_is_not_cancelled(client, tuner, plex):
    pid = _series_pass(n=2)
    first = db.one("SELECT id FROM recordings ORDER BY id")["id"]
    real = passes._upcoming

    def stale(p):
        got = real(p)
        # It starts after the list was made.
        with db.tx() as c:
            c.execute("UPDATE recordings SET state = 'recording' WHERE id = ?", (first,))
        return got
    passes._upcoming = stale
    try:
        assert passes.cancel_upcoming(pid) == 1
    finally:
        passes._upcoming = real
    assert _states() == ["recording"]


def test_the_pass_is_paused_while_it_cancels_and_back_if_plex_fails(client, synced, tuner):
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    passes.run_passes()
    pid = db.one("SELECT id FROM passes")["id"]
    db.set_setting("plex_url", "http://127.0.0.1:1")
    assert client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"}).status_code == 502
    assert db.one("SELECT enabled FROM passes")["enabled"] == 1


def test_the_pass_is_disabled_before_the_cancelling_starts(client, tuner, plex):
    pid = _series_pass(n=1)
    seen = []
    real = passes.cancel_upcoming
    from app.routes import passes as pr
    pr.passes.cancel_upcoming = lambda p: seen.append(
        db.one("SELECT enabled FROM passes WHERE id = ?", (p,))["enabled"]) or 1
    try:
        client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"})
    finally:
        pr.passes.cancel_upcoming = real
    assert seen == [0]


def _team_pass_with_grabs():
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    passes.run_passes()
    return db.one("SELECT id FROM passes")["id"]


def test_a_keyless_plex_booking_is_kept_linked_and_not_counted(client, synced, tuner):
    pid = _team_pass_with_grabs()
    n = db.one("SELECT COUNT(*) c FROM our_grabs")["c"]
    with db.tx() as c:
        c.execute("UPDATE our_grabs SET subscription = NULL")
    before = dict(fake_plex.STATE.subscriptions)
    assert client.get("/api/tv/recordings").json()["passes"][0]["upcoming"] == 0
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"}).json()
    assert r["cancelled"] == 0 and f"{n} booking(s) in Plex have to be removed" in r["message"]
    assert fake_plex.STATE.subscriptions == before
    assert db.one("SELECT COUNT(*) c FROM our_grabs WHERE pass_id = ?", (pid,))["c"] == n
    lost = client.get("/api/tv/passes/lost").json()["lost"]
    assert lost and lost[0]["upcoming"] == n


def test_a_partial_plex_failure_says_how_many_went_and_keeps_the_pass_paused(
        client, synced, tuner, monkeypatch):
    from app.plex import Plex
    pid = _team_pass_with_grabs()
    if db.one("SELECT COUNT(*) c FROM our_grabs")["c"] < 2:
        # The fixture guide has one game for this team: add a second booking.
        g = dict(db.one("SELECT * FROM our_grabs"))
        sub = dict(fake_plex.STATE.subscriptions[g["subscription"]])
        fake_plex.STATE.subscriptions["extra-1"] = sub
        g.update(airing_id="extra-airing", subscription="extra-1")
        with db.tx() as c:
            c.execute("INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn, "
                      "begins_at, source, subscription, pass_id, pass_uid, created_at) "
                      "VALUES (:airing_id, :program_guid, :title, :channel_vcn, :begins_at, "
                      ":source, :subscription, :pass_id, :pass_uid, :created_at)", g)
    n = db.one("SELECT COUNT(*) c FROM our_grabs")["c"]
    real, calls = Plex.delete_subscription, []

    def flaky(self, key):
        calls.append(key)
        if len(calls) == 2:
            raise RuntimeError("Plex went away")
        return real(self, key)
    monkeypatch.setattr(Plex, "delete_subscription", flaky)
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"})
    assert r.status_code == 502
    assert "1 were already cancelled" in r.json()["error"] and r.json()["cancelled"] == 1
    assert db.one("SELECT enabled FROM passes")["enabled"] == 0
    assert db.one("SELECT COUNT(*) c FROM our_grabs")["c"] == n - 1


def test_a_failing_recorder_is_not_reported_as_plex(client, tuner, plex, monkeypatch):
    pid = _series_pass(n=3)
    real, calls = recorder.cancel, []

    def broken(rid):
        calls.append(rid)
        if len(calls) == 2:
            raise OSError("disk")
        return real(rid)
    monkeypatch.setattr(recorder, "cancel", broken)
    r = client.post(f"/api/tv/passes/{pid}/delete", data={"cancel_upcoming": "1"})
    assert r.status_code == 502
    e = r.json()["error"]
    assert "A recording could not be cancelled" in e and "1 were already cancelled" in e
    assert "Plex could not" not in e


def test_the_count_leaves_out_what_is_about_to_start(client, tuner, plex):
    _series_pass(n=3)
    with db.tx() as c:
        c.execute("UPDATE recordings SET starts_at = ? WHERE id = (SELECT MIN(id) FROM recordings)",
                  (int(time.time()) - 5,))
    assert client.get("/api/tv/recordings").json()["passes"][0]["upcoming"] == 2


def test_a_booking_run_that_began_before_the_delete_books_nothing_more(client, synced, tuner):
    team = db.one("SELECT id, name FROM teams ORDER BY id LIMIT 1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, uid, enabled, created_at) "
                  "VALUES ('team', ?, ?, 'u1', 1, 0)", (team["id"], team["name"]))
    rows = db.query("SELECT * FROM passes WHERE enabled = 1")
    with db.tx() as c:
        c.execute("DELETE FROM passes")
    out = []
    from app.plex import Plex
    with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
        passes._evaluate(rows, plex, False, out)
    assert not [d for d in out if d["action"] == "scheduled"]
    assert db.one("SELECT COUNT(*) c FROM our_grabs")["c"] == 0 == len(fake_plex.STATE.subscriptions)


def test_changing_recorder_never_deletes_a_looked_up_plex_rule(client, synced, tuner):
    pid = _team_pass_with_grabs()
    with db.tx() as c:
        c.execute("UPDATE our_grabs SET subscription = NULL")
        c.execute("UPDATE passes SET recorder = 'couchelephant'")
    before = dict(fake_plex.STATE.subscriptions)
    from app.plex import Plex
    with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
        assert passes.rehome(db.one("SELECT * FROM passes WHERE id = ?", (pid,)), plex) == 0
    assert fake_plex.STATE.subscriptions == before


def test_the_cancel_is_refused_for_a_recording_that_has_started(client, tuner, plex):
    _series_pass(n=1)
    rid = db.one("SELECT id FROM recordings")["id"]
    with db.tx() as c:
        c.execute("UPDATE recordings SET starts_at = ?", (int(time.time()) - 5,))
    assert recorder.cancel_if_waiting(rid) is False
    assert db.one("SELECT COUNT(*) c FROM recordings")["c"] == 1


def test_a_lost_pass_with_no_show_name_cannot_be_followed(client, tuner, plex):
    pid = _series_pass(n=2)
    with db.tx() as c:
        c.execute("UPDATE recordings SET show_title = NULL, section = NULL")
    client.post(f"/api/tv/passes/{pid}/delete")
    lost = client.get("/api/tv/passes/lost").json()["lost"][0]
    assert lost["can_follow"] is False


def test_the_usual_delete_of_a_lost_row_cancels_it(client, tuner, plex):
    pid = _lost(client, n=2)
    r = client.post(f"/api/tv/passes/lost:{pid}/delete", data={"cancel_upcoming": "1"}).json()
    assert r["ok"] and r["cancelled"] == 2 and _states() == []


def test_a_lost_row_deleted_the_usual_way_without_cancelling_changes_nothing(client, tuner, plex):
    pid = _lost(client, n=2)
    r = client.post(f"/api/tv/passes/lost:{pid}/delete", data={"cancel_upcoming": "0"}).json()
    assert r["ok"] and r["cancelled"] == 0 and _states() == ["scheduled"] * 2
