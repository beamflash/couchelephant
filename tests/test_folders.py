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
import shlex
import stat
import threading
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


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """No state carried between tests, and no thread left over from one."""
    folders.settle()
    monkeypatch.setattr(folders, "_last_run", 0.0)
    monkeypatch.setattr(folders, "_no_squash", False)
    folders._booking.clear()
    yield
    folders.settle()


@pytest.fixture
def squash(monkeypatch):
    """Folders we cannot write, as root squash over NFS would make them. Both
    the stat check and the real write test refuse them, and what is inside."""
    denied = set()
    real, quick = folders.writable, folders.quick_writable

    def hit(path):
        path = os.path.normpath(path)
        return any(path == d or path.startswith(d + os.sep) for d in denied)
    monkeypatch.setattr(folders, "writable", lambda p: False if hit(p) else real(p))
    monkeypatch.setattr(folders, "quick_writable", lambda p: False if hit(p) else quick(p))
    return denied


def _notice():
    rows = [n for n in health.open_notices() if n["code"] == health.FOLDER_BLOCKED]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _plex_paths(**kw):
    db.set_setting("folders_plex_roots", json.dumps(
        kw or {"shows": "/mnt/user/tv", "movies": "/mnt/user/films"}))


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
        assert folders.quick_writable(str(d)) is False
    finally:
        d.chmod(stat.S_IRWXU)


def test_a_test_file_that_cannot_be_removed_is_a_problem_and_never_piles_up(
        tmp_path, monkeypatch):
    def refuse(path):
        raise PermissionError(13, "no", path)
    monkeypatch.setattr(os, "remove", refuse)
    assert folders.writable(str(tmp_path)) is False
    assert folders.writable(str(tmp_path)) is False
    assert len(os.listdir(tmp_path)) == 1      # the same file, reused


def test_the_nearest_existing_parent_is_used_for_a_folder_not_made_yet(tmp_path):
    (tmp_path / "a").mkdir()
    deep = str(tmp_path / "a" / "b" / "c")
    assert folders.nearest_existing(deep) == str(tmp_path / "a")


# ---- the stat check ----

class _St:
    def __init__(self, uid, gid, mode):
        self.st_uid, self.st_gid, self.st_mode = uid, gid, mode


def test_the_mode_bits_are_read_for_owner_group_and_others():
    plex_755 = _St(99, 100, 0o40755)
    assert folders._mode_allows(plex_755, 99, {100}) is True        # the owner
    assert folders._mode_allows(plex_755, 1000, {100}) is False     # group has no write
    assert folders._mode_allows(plex_755, 1000, {5}) is False       # nor others
    assert folders._mode_allows(_St(99, 100, 0o40775), 1000, {100}) is True
    assert folders._mode_allows(_St(99, 100, 0o40777), 1000, {5}) is True
    assert folders._mode_allows(_St(99, 100, 0o40720), 1000, {100}) is False  # no search bit


def test_root_over_nfs_is_read_as_squashed_to_nobody(tmp_path, monkeypatch):
    d = tmp_path / "nfl"
    d.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(folders, "_on_nfs", lambda p: True)
    d.chmod(0o755)
    monkeypatch.setattr(os, "stat", lambda p, _s=os.stat: _St(99, 100, 0o40755)
                        if str(p) == str(d) else _s(p))
    assert folders.quick_writable(str(d)) is False
    # Until a real write has shown the NAS does not squash root.
    monkeypatch.setattr(folders, "_no_squash", True)
    assert folders.quick_writable(str(d)) is True


def test_root_off_nfs_is_not_second_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(folders, "_on_nfs", lambda p: False)
    assert folders.quick_writable(str(tmp_path)) is True


def test_the_scan_never_writes_into_a_folder(lib, monkeypatch):
    def boom(path):
        raise AssertionError("the scan wrote into " + path)
    monkeypatch.setattr(folders, "writable", boom)
    assert folders.scan() == []


def test_a_nas_own_folders_and_dot_folders_are_not_scanned(lib, squash):
    tv = os.path.join(lib, "tv")
    for n in ("@eaDir", ".snapshots", "#recycle", "lost+found", ".hidden"):
        os.makedirs(os.path.join(tv, n))
        squash.add(os.path.join(tv, n))
    assert folders.scan() == []


# ---- the fix text ----

def test_the_fix_names_the_folder_in_the_library_not_the_container_path(lib):
    _plex_paths()
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    d = folders.describe(nfl)
    assert lib not in d["label"] and lib not in d["cmd"]
    assert d["label"] == 'the folder "NFL Football (2002)" in the TV library'
    assert shlex.split(d["cmd"]) == ["chmod", "-R", "a+rwX", "/mnt/user/tv/NFL Football (2002)"]
    fix = folders.fix_command([nfl])
    assert fix.startswith("On the machine that holds the library, run: chmod")


def test_without_plexs_path_the_fix_is_relative_to_the_library_folder(lib):
    d = folders.describe(os.path.join(lib, "tv", "NFL Football (2002)"))
    assert "cd to your TV library folder" in d["cmd"]
    assert lib not in d["cmd"]


def test_a_folder_name_is_quoted_so_it_cannot_run_anything(lib):
    _plex_paths()
    for name in ("a$b", "a`c`", "wow!", "it's", 'say "hi"', "x; rm -rf ~", "$(id)"):
        d = folders.describe(os.path.join(lib, "tv", name))
        argv = shlex.split(d["cmd"])
        assert argv == ["chmod", "-R", "a+rwX", "/mnt/user/tv/" + name], name


def test_the_library_folder_itself_never_gets_a_recursive_chmod(lib):
    _plex_paths()
    root = os.path.join(lib, "tv")
    d = folders.describe(root)
    assert d["label"] == "the TV library folder"
    assert "-R" not in d["cmd"]
    assert shlex.split(d["cmd"]) == ["chmod", "a+rwx", "/mnt/user/tv"]
    fix = folders.fix_command([root])
    assert "do not use -R on all of it" in fix


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
    folders.refresh(rescan=True)        # also learns Plex's own path for the library
    folders.refresh(rescan=True)
    n = _notice()
    assert n["severity"] == "warn"
    assert "NFL Football (2002)" in n["detail"] and "Season 02" in n["detail"]
    assert lib not in n["detail"]
    assert "chmod -R a+rwX '/media/video/tv/NFL Football (2002)'" in n["detail"]
    assert "On the machine that holds the library" in n["detail"]
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
    folders.settle()
    assert "NFL Football" in _notice()["detail"]
    # A new bad folder an hour later is not looked for yet.
    squash.add(os.path.join(lib, "tv", "Show A (2001)"))
    folders.tick(now + 3600)
    folders.settle()
    assert "Show A" not in _notice()["detail"]
    folders.tick(now + 86400 + 60)
    folders.settle()
    assert "Show A" in _notice()["detail"]


def test_nothing_is_scanned_before_a_library_is_chosen(plex):
    folders.refresh(rescan=True)
    assert _notice() is None


def test_an_open_notice_clears_when_the_library_is_gone(lib, squash):
    squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
    folders.refresh(rescan=True)
    assert _notice()
    db.set_setting("recorder_shows_folder", "")
    db.set_setting("recorder_movies_folder", "")
    folders.refresh()
    assert _notice() is None


def test_the_daily_scan_runs_on_its_own_thread_and_the_loop_does_not_wait(lib, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(budget):
        started.set()
        release.wait(10)
        return [], True
    monkeypatch.setattr(folders, "_scan", slow)
    t0 = time.time()
    folders.tick(int(time.time()))
    assert time.time() - t0 < 0.5
    assert started.wait(5)
    # The state lock is not held across the disk work: a booking can take it.
    assert folders._lock.acquire(timeout=0.5)
    folders._lock.release()
    folders.rescan_soon()          # a second ask meanwhile does not start a second scan
    release.set()
    folders.settle()


def test_a_scan_that_runs_out_of_time_stops_and_says_so(lib, monkeypatch):
    got = folders._scan(-1)
    assert got[1] is False


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
    rec_id = recorder.book(_airing("nfl", "g-nfl", "5.1", now + 3 * 86400,
                                   show="NFL Football", **kw))
    folders.settle()                   # the check runs on its own thread
    return rec_id


def test_booking_into_a_blocked_folder_warns_days_ahead(tuner, lib, squash):
    nfl = os.path.join(lib, "tv", "NFL Football (2002)")
    squash.add(nfl)
    rec_id = _book_nfl(lib)
    n = _notice()
    assert n["severity"] == "warn"
    assert "NFL Football (2002)" in n["detail"] and "Chiefs at Broncos" in n["detail"]
    warning = folders.booking_warning(recorder.get(rec_id))
    assert "NFL Football (2002)" in warning and lib not in warning


def test_a_folder_not_made_yet_is_judged_by_its_nearest_parent(tuner, lib, squash):
    tv = os.path.join(lib, "tv")
    squash.add(tv)
    now = int(time.time())
    recorder.book(_airing("new", "g-new", "5.1", now + 86400, show="Brand New Show"))
    folders.settle()
    # Only the library folder is in the way, and that is not a recursive chmod.
    assert "the TV library folder" in _notice()["detail"]
    assert "chmod -R" not in _notice()["detail"]


def test_a_booking_into_a_writable_place_raises_nothing(tuner, lib, squash):
    _book_nfl(lib)
    assert _notice() is None


def test_cancelling_the_booking_clears_its_notice(tuner, lib, squash):
    squash.add(os.path.join(lib, "tv", "NFL Football (2002)"))
    rec_id = _book_nfl(lib)
    assert _notice()
    recorder.cancel(rec_id)
    folders.publish()
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
    assert "NFL Football (2002)" in out["warning"]


def test_a_hung_disk_never_holds_a_booking_or_a_request(tuner, lib, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(folders, "writable", lambda p: release.wait(10) or True)
    monkeypatch.setattr(folders, "REQUEST_WAIT_SECONDS", 0.3)
    t0 = time.time()
    rec_id = _book_nfl_no_wait()
    assert time.time() - t0 < 1.0                 # booking did not wait on the disk
    t0 = time.time()
    assert folders.booking_warning(recorder.get(rec_id)) is None
    assert time.time() - t0 < 1.5                 # nor did the request
    release.set()
    folders.settle()


def _book_nfl_no_wait():
    now = int(time.time())
    return recorder.book(_airing("nflh", "g-nflh", "5.1", now + 86400, show="NFL Football"))


def test_the_library_is_listed_once_however_many_recordings_are_booked(tuner, lib, monkeypatch):
    now = int(time.time())
    for i in range(4):
        recorder.book(_airing(f"s{i}", f"g-s{i}", "5.1", now + 3600 * (i + 1),
                              show=f"Show Number {i}"))
    folders.settle()
    seen = []
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p=".": (seen.append(str(p)), real(p))[1])
    folders.refresh()
    assert seen.count(os.path.join(lib, "tv")) <= 1


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
    assert got["error"].startswith(
        'CouchElephant cannot write to the folder "NFL Football (2002)')
    assert lib not in got["error"]
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
    folders.settle()
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


def test_retry_now_starts_the_copy_and_says_so(tuner, lib, client):
    rid = _failed(tuner, lib)
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["outcome"] == "started" and "Copying" in got["message"]
    _until(lambda: recorder.get(rid)["state"] == "in_plex")
    handover._worker.join()


def test_retry_now_says_when_another_copy_is_first(tuner, lib, client, monkeypatch):
    rid = _failed(tuner, lib)
    monkeypatch.setattr(handover, "tick", lambda *a, **k: None)
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["outcome"] == "queued"


def test_retry_now_says_when_it_is_already_copying(tuner, lib, client):
    rid = _failed(tuner, lib)
    recorder._set(rid, state="moving")
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["outcome"] == "copying"


def test_retry_now_says_when_somebody_watched_it_a_moment_ago(tuner, lib, client):
    rid = _failed(tuner, lib)
    recorder._set(rid, watched_at=int(time.time()))
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["outcome"] == "watched"
    assert recorder.get(rid)["tried_at"] != 0      # nothing was started behind their back


def test_retry_now_says_when_the_viewing_copy_is_still_converting(tuner, lib, client,
                                                                 monkeypatch):
    rid = _failed(tuner, lib)
    monkeypatch.setattr(recorder, "busy", lambda i: True)
    got = client.post(f"/api/tv/recordings/{rid}/retry").json()
    assert got["outcome"] == "converting"


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


def _entry(tmp_path, env, uid="0", chown_fails=False, dri=()):
    """Run the entrypoint with stand-ins that print what they would do."""
    import subprocess
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "id").write_text(f"#!/bin/sh\necho {uid}\n")
    (bin_ / "setpriv").write_text('#!/bin/sh\necho "setpriv $*"\n')
    (bin_ / "chown").write_text('#!/bin/sh\necho "chown $*"\n'
                                + ("exit 1\n" if chown_fails else ""))
    for f in bin_.iterdir():
        f.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "a.db").write_text("x")
    gpu = tmp_path / "dri"
    gpu.mkdir(exist_ok=True)
    for n in dri:
        (gpu / n).write_text("")
    return subprocess.run(
        ["sh", ENTRY, "echo", "started"], capture_output=True, text=True,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "CE_OWNED_DIRS": str(data),
             "CE_DRI_DIR": str(gpu), **env})


def test_with_no_puid_the_container_stays_root(tmp_path):
    out = _entry(tmp_path, {})
    assert out.returncode == 0
    assert "setpriv" not in out.stdout and "started" in out.stdout


def test_puid_and_pgid_run_the_app_as_that_user(tmp_path):
    out = _entry(tmp_path, {"PUID": "99", "PGID": "100"})
    assert "setpriv --reuid=99 --regid=100 --groups=100 echo started" in out.stdout
    assert "chown 99:100" in out.stdout         # our own data, never the library


def test_the_gpu_group_is_kept_so_hardware_encoding_still_works(tmp_path):
    out = _entry(tmp_path, {"PUID": "99", "PGID": "100"}, dri=("renderD128", "card0"))
    gid = os.stat(tmp_path / "dri" / "renderD128").st_gid
    want = "100" if gid == 100 else f"100,{gid}"
    assert f"--groups={want} echo started" in out.stdout
    assert "--clear-groups" not in out.stdout


def test_only_files_not_already_ours_are_chowned(tmp_path):
    out = _entry(tmp_path, {"PUID": str(os.getuid()), "PGID": str(os.getgid())})
    assert "chown" not in out.stdout            # everything is already ours
    assert "setpriv" in out.stdout


def test_a_chown_that_fails_warns_and_the_app_still_starts(tmp_path):
    out = _entry(tmp_path, {"PUID": "99", "PGID": "100"}, chown_fails=True)
    assert out.returncode == 0
    assert "setpriv --reuid=99" in out.stdout and "started" in out.stdout
    assert "could not give" in out.stderr and "Carrying on" in out.stderr


def test_pgid_defaults_to_puid(tmp_path):
    out = _entry(tmp_path, {"PUID": "99"})
    assert "--regid=99" in out.stdout


def test_a_bad_puid_is_refused_in_words(tmp_path):
    out = _entry(tmp_path, {"PUID": "plex"})
    assert out.returncode != 0
    assert "PUID" in out.stderr and "number" in out.stderr


def test_the_library_is_never_chowned(tmp_path):
    assert "/library" not in open(ENTRY).read()


# ---- one notice, and who is told ----

def _dest(url, events=None):
    return notify.save_destination(
        name="Apps", kind="discord", events=events or [health.FOLDER_BLOCKED],
        remind_hours=24, webhook=f"{url}/api/webhooks/1/a")


def _folder_notice(severity, now, code=health.FOLDER_BLOCKED):
    health.record([{"code": code, "severity": severity, "title": "Folder",
                    "detail": "NFL Football (2002)", "hint": "h"}], now,
                  owns=health.FOLDER_CODES)


@pytest.fixture
def chat(monkeypatch):
    url = fake_chat.start()
    monkeypatch.setattr(notify, "DISCORD_HOSTS", frozenset({"127.0.0.1", "localhost"}))
    monkeypatch.setattr(notify, "DISCORD_SCHEMES", frozenset({"http"}))
    yield url
    fake_chat.stop()


def test_red_then_warn_then_red_is_pushed_both_times(chat):
    _dest(chat)
    now = int(time.time())
    _folder_notice("warn", now)
    assert notify.dispatch(now=now) == 0
    _folder_notice("bad", now + 10)
    assert notify.dispatch(now=now + 10) == 1
    _folder_notice("warn", now + 20)                 # fixed for now, a risk again
    assert notify.dispatch(now=now + 20) == 0
    _folder_notice("bad", now + 30)
    assert notify.dispatch(now=now + 30) == 1
    assert len(fake_chat.discord_sent()) == 2


def test_resolved_then_red_again_is_pushed_again(chat):
    _dest(chat)
    now = int(time.time())
    _folder_notice("bad", now)
    assert notify.dispatch(now=now) == 1
    health.record([], now + 10, owns=health.FOLDER_CODES)
    assert notify.dispatch(now=now + 10) == 1        # the "cleared" message
    _folder_notice("bad", now + 20)
    assert notify.dispatch(now=now + 20) == 1
    assert len(fake_chat.discord_sent()) == 3


def test_resolved_and_red_again_before_the_next_pass_is_still_news(chat):
    _dest(chat)
    now = int(time.time())
    _folder_notice("bad", now)
    notify.dispatch(now=now)
    health.record([], now + 10, owns=health.FOLDER_CODES)
    _folder_notice("bad", now + 20)                  # no dispatch in between
    assert notify.dispatch(now=now + 20) == 1


def test_a_new_destination_hears_a_folder_that_was_only_a_risk_when_it_turns_red(chat):
    now = int(time.time())
    _folder_notice("warn", now)
    _dest(chat)                                      # added while it is only a risk
    assert notify.dispatch(now=now) == 0
    _folder_notice("bad", now + 10)
    assert notify.dispatch(now=now + 10) == 1


def test_existing_destinations_that_hear_every_fault_get_the_folder_one(chat):
    every = [c for c, _ in notify.FAULTS if c != health.FOLDER_BLOCKED]
    full = _dest(chat, events=every)
    narrow = _dest(chat, events=every[:-1])
    db.set_setting("mig_folder_event", "")
    notify.add_folder_event()
    assert health.FOLDER_BLOCKED in notify.split_events(notify.get_destination(full)["events"])
    assert health.FOLDER_BLOCKED not in notify.split_events(
        notify.get_destination(narrow)["events"])
    notify.add_folder_event()                        # once only
