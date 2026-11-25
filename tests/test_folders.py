"""Warning before a Plex library folder blocks a handover.

Plex makes a show folder as its own user, mode 755. CouchElephant, running as
somebody else, cannot copy a recording into it, and the first anyone hears of
that is a game that never reaches Plex. These tests pin the early warnings.

A folder CouchElephant cannot write is faked two ways: a real chmod (skipped
when the suite runs as root, who can write anywhere) and a stand-in for the
write test, which is what stands in for NFS root squash.
"""
import json
import os
import stat
import time

import pytest

from app import db, folders, handover, health, notify, recorder
from tests import fake_chat
from tests.test_recorder import _airing, _record, _until

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture
def lib(plex):
    """A TV library with two shows, each with a season, and a movies folder."""
    root = handover.LIB_DIR
    for p in ("tv/Show A (2001)/Season 01", "tv/Show A (2001)/Season 02",
              "tv/NFL Football (2002)/Season 2026", "films/Movie One (1999)"):
        os.makedirs(os.path.join(root, p))
    db.set_setting("recorder_shows_folder", "tv")
    db.set_setting("recorder_shows_section", "2")
    db.set_setting("recorder_movies_folder", "films")
    db.set_setting("recorder_movies_section", "1")
    return root


@pytest.fixture
def squash(monkeypatch):
    """Folders the write test refuses, as root squash over NFS would."""
    denied = set()
    real = folders.writable

    def fake(path):
        path = os.path.normpath(path)
        if any(path == d or path.startswith(d + os.sep) for d in denied):
            return False
        return real(path)
    monkeypatch.setattr(folders, "writable", fake)
    return denied


def _notice():
    rows = [n for n in health.open_notices() if n["code"] == health.FOLDER_BLOCKED]
    assert len(rows) <= 1
    return rows[0] if rows else None


# ---- the write test ----

def test_a_folder_we_can_write_passes_and_leaves_nothing(tmp_path):
    assert folders.writable(str(tmp_path)) is True
    assert os.listdir(tmp_path) == []


def test_a_missing_folder_is_not_writable(tmp_path):
    assert folders.writable(str(tmp_path / "nope")) is False


@pytest.mark.skipif(IS_ROOT, reason="root writes anywhere")
def test_a_folder_with_no_write_bit_fails_the_real_test(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert folders.writable(str(d)) is False
    finally:
        d.chmod(stat.S_IRWXU)


def test_the_nearest_existing_parent_is_used_for_a_folder_not_made_yet(tmp_path):
    (tmp_path / "a").mkdir()
    deep = str(tmp_path / "a" / "b" / "c")
    assert folders.nearest_existing(deep) == str(tmp_path / "a")


# ---- the fix text ----

def test_the_fix_is_a_command_to_copy_for_each_folder():
    fix = folders.fix_command(["/mnt/user/tv/NFL Football (2002)", "/mnt/user/tv/B"])
    assert 'chmod -R a+rwX "/mnt/user/tv/NFL Football (2002)"' in fix
    assert 'chmod -R a+rwX "/mnt/user/tv/B"' in fix


def test_unraid_is_named_with_its_new_permissions_tool(monkeypatch):
    monkeypatch.setattr(folders, "is_unraid", lambda: False)
    assert "Tools, New Permissions" in folders.unraid_note()
    assert folders.unraid_note().lower().count("on unraid") == 1
    monkeypatch.setattr(folders, "is_unraid", lambda: True)
    assert "Tools, New Permissions" in folders.unraid_note()


def test_unraid_is_found_by_its_version_file(monkeypatch, tmp_path):
    f = tmp_path / "unraid-version"
    monkeypatch.setattr(folders, "UNRAID_FILE", str(f))
    assert folders.is_unraid() is False
    f.write_text('version="7.0"\n')
    assert folders.is_unraid() is True


# ---- which user ----

def test_setup_tells_you_to_match_the_user_that_made_the_folders(lib, monkeypatch):
    monkeypatch.setattr(folders, "_owner", lambda p: (99, 100))
    monkeypatch.setattr(folders, "_me", lambda: (0, 0))
    advice = folders.user_advice()
    assert "PUID=99" in advice and "PGID=100" in advice


def test_no_advice_when_we_already_are_that_user(lib, monkeypatch):
    monkeypatch.setattr(folders, "_owner", lambda p: (99, 100))
    monkeypatch.setattr(folders, "_me", lambda: (99, 100))
    assert folders.user_advice() is None


def test_the_library_suggest_answer_carries_the_advice(lib, client, monkeypatch):
    monkeypatch.setattr(folders, "_owner", lambda p: (99, 100))
    monkeypatch.setattr(folders, "_me", lambda: (0, 0))
    got = client.get("/api/recorder/library-suggest").json()
    assert "PUID=99" in (got.get("user_advice") or "")


# ---- the scan: setup, and once a day ----

def test_the_scan_finds_show_and_season_folders_we_cannot_write(lib, squash):
    squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
    squash.add(os.path.join(lib, "tv", "Show A (2001)", "Season 02"))
    squash.add(os.path.join(lib, "films", "Movie One (1999)"))
    found = folders.scan()
    # A folder that refuses us refuses what is inside it too.
    assert sorted(found) == sorted([
        os.path.join(lib, "tv", "NFL Football (2002)"),
        os.path.join(lib, "tv", "NFL Football (2002)", "Season 2026"),
        os.path.join(lib, "tv", "Show A (2001)", "Season 02"),
        os.path.join(lib, "films", "Movie One (1999)")])


def test_a_writable_library_scans_clean(lib):
    assert folders.scan() == []


def test_one_notice_lists_every_folder_and_is_not_repeated(lib, squash):
    a = os.path.join(lib, "tv", "NFL Football (2002)")
    b = os.path.join(lib, "tv", "Show A (2001)", "Season 02")
    squash.update({a, b})
    folders.refresh(rescan=True)
    folders.refresh(rescan=True)
    n = _notice()
    assert n["severity"] == "warn"
    assert a in n["detail"] and b in n["detail"]
    assert f'chmod -R a+rwX "{a}"' in n["detail"]
    assert "Tools, New Permissions" in n["hint"]
    assert db.one("SELECT COUNT(*) c FROM notices WHERE code = ?",
                  (health.FOLDER_BLOCKED,))["c"] == 1


def test_the_notice_clears_when_the_folder_is_fixed(lib, squash):
    a = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(a)
    folders.refresh(rescan=True)
    assert _notice()
    squash.clear()
    folders.refresh()          # no rescan: the folders already known are checked again
    assert _notice() is None


def test_the_daily_pass_scans_once_a_day(lib, squash):
    a = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(a)
    now = int(time.time())
    folders.tick(now)
    assert a in _notice()["detail"]
    # A new bad folder an hour later is not looked for yet.
    b = os.path.join(lib, "tv", "Show A (2001)")
    squash.add(b)
    folders.tick(now + 3600)
    assert b not in _notice()["detail"]
    folders.tick(now + 86400 + 60)
    assert b in _notice()["detail"]


def test_nothing_is_scanned_before_a_library_is_chosen(plex):
    folders.refresh(rescan=True)
    assert _notice() is None


def test_we_never_change_a_permission(lib, squash, monkeypatch):
    calls = []
    for name in ("chmod", "chown", "lchown"):
        monkeypatch.setattr(os, name, lambda *a, _n=name, **k: calls.append(_n))
    squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
    folders.refresh(rescan=True)
    assert calls == []


# ---- when a recording is booked ----

def _book_nfl(lib, **kw):
    now = int(time.time())
    return recorder.book(_airing("nfl", "g-nfl", "5.1", now + 3 * 86400,
                                 show="NFL Football", **kw))


def test_booking_into_a_blocked_folder_warns_days_ahead(tuner, lib, squash):
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(nfl)
    rec_id = _book_nfl(lib)
    n = _notice()
    assert n["severity"] == "warn"
    assert nfl in n["detail"] and "Chiefs at Broncos" in n["detail"]
    assert folders.booking_warning(recorder.get(rec_id)) is not None
    assert nfl in folders.booking_warning(recorder.get(rec_id))


def test_a_folder_not_made_yet_is_judged_by_its_nearest_parent(tuner, lib, squash):
    tv = os.path.join(lib, "tv")
    squash.add(tv)
    now = int(time.time())
    recorder.book(_airing("new", "g-new", "5.1", now + 86400, show="Brand New Show"))
    assert tv in _notice()["detail"]


def test_a_booking_into_a_writable_place_raises_nothing(tuner, lib, squash):
    _book_nfl(lib)
    assert _notice() is None


def test_cancelling_the_booking_clears_its_notice(tuner, lib, squash):
    squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
    rec_id = _book_nfl(lib)
    assert _notice()
    recorder.cancel(rec_id)
    folders.refresh()
    assert _notice() is None


def test_the_record_answer_carries_the_warning(tuner, lib, squash, client):
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(nfl)
    now = int(time.time())
    _airing("nfl2", "g-nfl2", "5.1", now + 86400, show="NFL Football")
    row = db.one("SELECT a.*, p.title, p.grandparent_title FROM airings a "
                 "JOIN programs p ON p.guid = a.program_guid WHERE a.id = 'nfl2'")
    from app.routes import record
    out = json.loads(record._record_ourselves(row, {}).body)
    assert out["ok"] is True
    assert nfl in out["warning"]


# ---- when the recording starts ----

def test_the_start_turns_the_notice_red_and_pushes_it(tuner, lib, squash, monkeypatch):
    url = fake_chat.start()
    monkeypatch.setattr(notify, "DISCORD_HOSTS", frozenset({"127.0.0.1", "localhost"}))
    monkeypatch.setattr(notify, "DISCORD_SCHEMES", frozenset({"http"}))
    try:
        notify.save_destination(name="Apps", kind="discord",
                                events=[health.FOLDER_BLOCKED], remind_hours=24,
                                webhook=f"{url}/api/webhooks/1/a")
        squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
        rec_id = _book_nfl(lib)
        assert _notice()["severity"] == "warn"
        # A warning is not worth a push.
        assert notify.dispatch() == 0
        recorder._set(rec_id, state="recording", started_at=int(time.time()))
        folders.on_start(recorder.get(rec_id))
        assert _notice()["severity"] == "bad"
        assert notify.dispatch() == 1
        sent = str(fake_chat.discord_sent())
        assert "NFL Football (2002)" in sent
        assert notify.dispatch() == 0     # once, not every pass
    finally:
        fake_chat.stop()


def test_the_recorder_checks_the_folder_as_the_recording_starts(tuner, lib, squash,
                                                                 monkeypatch):
    seen = []
    monkeypatch.setattr(folders, "on_start", lambda rec: seen.append(rec["id"]))
    now = int(time.time())
    rec_id = recorder.book(_airing("now", "g-now", "5.1", now - 30))
    recorder.tick()
    deadline = time.time() + 10
    while not seen and time.time() < deadline:
        time.sleep(0.05)
    assert seen == [rec_id]
    recorder.cancel(rec_id)


# ---- after a failed handover ----

def test_a_failed_copy_says_which_folder_in_plain_words(tuner, lib, squash, monkeypatch):
    rec = _record(show="NFL Football")
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(nfl)

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied", dst)
    monkeypatch.setattr(handover.shutil, "copyfile", refuse)
    handover.tick(wait=True)
    got = recorder.get(rec["id"])
    assert got["state"] == "finished"
    assert got["error"].startswith("CouchElephant cannot write to the folder ")
    assert nfl in got["error"]
    assert "Errno" not in got["error"] and "Permission denied" not in got["error"]
    # A handover that failed for this is a problem right now.
    assert _notice()["severity"] == "bad"


def test_a_copy_that_works_clears_the_notice(tuner, lib, squash):
    rec = _record(show="NFL Football")
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(nfl)
    folders.on_start(recorder.get(rec["id"]))
    assert _notice()
    squash.clear()
    handover.tick(wait=True)
    assert recorder.get(rec["id"])["state"] == "in_plex"
    assert _notice() is None


# ---- Retry now ----

def _failed(tuner, lib):
    rec = _record(show="NFL Football")
    recorder._set(rec["id"], error="CouchElephant cannot write to the folder X",
                  tried_at=int(time.time()))
    return rec["id"]


def test_retry_now_makes_the_handover_due_at_once(tuner, lib, client, monkeypatch):
    rid = _failed(tuner, lib)
    assert handover.due(int(time.time())) == []
    monkeypatch.setattr(handover, "tick", lambda *a, **k: None)   # only "due" is asked
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["ok"] is True
    assert [r["id"] for r in handover.due(int(time.time()))] == [rid]


def test_retry_now_starts_the_copy(tuner, lib, client):
    rid = _failed(tuner, lib)
    client.post(f"/api/tv/recordings/{rid}/retry")
    _until(lambda: recorder.get(rid)["state"] == "in_plex")


def test_retry_now_refuses_what_is_not_waiting_to_retry(tuner, lib, client):
    rid = _failed(tuner, lib)
    recorder._set(rid, state="in_plex")
    r = client.post(f"/api/tv/recordings/{rid}/retry")
    assert r.status_code == 409
    assert client.post("/api/tv/recordings/99999/retry").status_code == 404


def test_the_list_says_why_a_handover_is_waiting(tuner, lib, client):
    rid = _failed(tuner, lib)
    rows = client.get("/api/tv/recordings").json()["recorded"]
    mine = next(r for r in rows if r["id"] == rid)
    assert mine["handover_error"].startswith("CouchElephant cannot write")
    assert 0 < mine["retry_in"] <= handover.RETRY_SECONDS


# ---- the container runs as the user Plex runs as ----

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "docker-entrypoint.sh")


def _entry(tmp_path, env, uid="0"):
    """Run the entrypoint with stand-ins that print what they would do."""
    import subprocess
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "id").write_text(f"#!/bin/sh\necho {uid}\n")
    (bin_ / "setpriv").write_text('#!/bin/sh\necho "setpriv $*"\n')
    (bin_ / "chown").write_text('#!/bin/sh\necho "chown $*"\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    out = subprocess.run(
        ["sh", ENTRY, "echo", "started"], capture_output=True, text=True,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "CE_OWNED_DIRS": str(data), **env})
    return out


def test_with_no_puid_the_container_stays_root(tmp_path):
    out = _entry(tmp_path, {})
    assert out.returncode == 0
    assert "setpriv" not in out.stdout and "started" in out.stdout


def test_puid_and_pgid_run_the_app_as_that_user(tmp_path):
    out = _entry(tmp_path, {"PUID": "99", "PGID": "100"})
    assert "setpriv --reuid=99 --regid=100 --clear-groups echo started" in out.stdout
    assert "chown -R 99:100" in out.stdout      # our own data, never the library


def test_pgid_defaults_to_puid(tmp_path):
    out = _entry(tmp_path, {"PUID": "99"})
    assert "--regid=99" in out.stdout


def test_a_bad_puid_is_refused_in_words(tmp_path):
    out = _entry(tmp_path, {"PUID": "plex"})
    assert out.returncode != 0
    assert "PUID" in out.stderr and "number" in out.stderr


def test_the_library_is_never_chowned(tmp_path):
    text = open(ENTRY).read()
    assert "/library" not in text
