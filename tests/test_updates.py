"""Noticing a newer release, and the one-click update. Never the real GitHub."""
import hashlib
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import auth, db, menubar, tray, updates
from app.routes._shared import VERSION


def _bump(version, by=1):
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + by)
    return ".".join(parts)


NEWER = _bump(VERSION)
INSTALLER = b"pretend installer bytes"


class _Feed(BaseHTTPRequestHandler):
    """Serves the release JSON at /releases and the files it points at."""
    files: dict = {}
    release: dict = {}
    status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/releases":
            body = json.dumps(self.release).encode()
            code = self.status
        elif self.path.lstrip("/") in self.files:
            body, code = self.files[self.path.lstrip("/")], 200
        else:
            body, code = b"nope", 404
        self.send_response(code)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def feed(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Feed)
    base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("COUCHELEPHANT_RELEASES_URL", base + "/releases")
    updates.reset()

    def publish(version=NEWER, names=(), sums=True, bad_hash=False, **extra):
        files, assets = {}, []
        for name in names:
            files[name] = INSTALLER
            assets.append({"name": name, "browser_download_url": f"{base}/{name}"})
        if sums:
            digest = "0" * 64 if bad_hash else hashlib.sha256(INSTALLER).hexdigest()
            files["SHA256SUMS"] = "".join(f"{digest}  {n}\n" for n in names).encode()
            assets.append({"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS"})
        _Feed.files = files
        _Feed.release = {"tag_name": f"v{version}", "html_url": f"{base}/page",
                         "draft": False, "prerelease": False, "assets": assets, **extra}
        _Feed.status = 200
        updates.reset()

    yield publish
    server.shutdown()
    server.server_close()
    updates.reset()


# ---------- noticing ----------

def test_a_newer_release_is_available(feed):
    feed()
    rel = updates.available()
    assert rel.version == NEWER and rel.url.endswith("/page")


def test_the_same_or_an_older_release_is_not(feed):
    feed(VERSION)
    assert updates.available() is None
    feed("0.0.1")
    assert updates.available() is None


def test_versions_compare_as_numbers():
    assert updates.newer("1.5.10", "1.5.9")
    assert not updates.newer("1.5.9", "1.5.10")
    assert not updates.newer("1.5", "1.5.0")
    assert not updates.newer("garbage", "1.5.0")


def test_drafts_and_prereleases_are_ignored(feed):
    feed(draft=True)
    assert updates.available() is None
    feed(prerelease=True)
    assert updates.available() is None


def test_a_dead_feed_gives_none_and_one_log_line(feed, caplog):
    feed()
    _Feed.status = 500
    with caplog.at_level(logging.WARNING, logger="couchelephant"):
        assert updates.latest(refresh=True) is None
        assert updates.available() is None
    assert len([r for r in caplog.records if "update check failed" in r.message]) == 1


def test_a_feed_nobody_answers_keeps_the_server_running(client, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_RELEASES_URL", "http://127.0.0.1:9/releases")
    updates.reset()
    r = client.get("/api/update")
    assert r.status_code == 200 and r.json()["latest"] is None
    updates.reset()


def test_the_check_is_off_with_an_empty_feed_url(monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_RELEASES_URL", "")
    updates.reset()
    assert updates.latest() is None


def test_how_follows_the_system(monkeypatch):
    monkeypatch.setattr(updates, "_in_docker", lambda: True)
    assert updates.how() == "docker"
    monkeypatch.setattr(updates, "_in_docker", lambda: False)
    for system, want in (("Windows", "click"), ("Darwin", "click"), ("Linux", "command")):
        monkeypatch.setattr(updates, "_system", lambda s=system: s)
        assert updates.how() == want


# ---------- the endpoint ----------

def test_the_endpoint_reports_the_release(client, feed, monkeypatch):
    monkeypatch.setattr(updates, "_in_docker", lambda: True)
    feed()
    data = client.get("/api/update").json()
    assert data["ok"] and data["current"] == VERSION and data["latest"] == NEWER
    assert data["how"] == "docker" and data["click"] is False


def test_a_release_without_the_installer_offers_no_click(client, feed, monkeypatch):
    monkeypatch.setattr(updates, "_in_docker", lambda: False)
    monkeypatch.setattr(updates, "_system", lambda: "Windows")
    feed(names=())
    data = client.get("/api/update").json()
    assert data["latest"] == NEWER and data["click"] is False
    r = client.post("/api/update/apply")
    assert r.status_code == 400 and "couchelephant.app" in r.json()["error"]


def test_apply_is_refused_in_docker_and_on_linux(client, feed, monkeypatch):
    feed()
    monkeypatch.setattr(updates, "_in_docker", lambda: True)
    r = client.post("/api/update/apply")
    assert r.status_code == 400 and "container" in r.json()["error"]
    monkeypatch.setattr(updates, "_in_docker", lambda: False)
    monkeypatch.setattr(updates, "_system", lambda: "Linux")
    r = client.post("/api/update/apply")
    assert r.status_code == 400 and "install.sh" in r.json()["error"]


def test_only_an_administrator_is_told_and_may_apply(client, feed):
    feed()
    db.set_setting("auth_mode", "local")
    member = auth.create_user("kid", "a-good-password", role="user")
    admin = auth.create_user("boss", "a-good-password", role="admin")
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(member))
    assert client.get("/api/update").json()["latest"] is None
    assert client.post("/api/update/apply").status_code == 403
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin))
    assert client.get("/api/update").json()["latest"] == NEWER
    client.cookies.clear()
    assert client.get("/api/update").status_code == 401


# ---------- Windows ----------

@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(updates, "_in_docker", lambda: False)
    monkeypatch.setattr(updates, "_system", lambda: "Windows")
    ran = []
    monkeypatch.setattr(updates, "_shell_execute_runas",
                        lambda path, params: ran.append((path, params)))
    return ran


def test_the_service_sends_a_windows_update_to_the_tray(client, feed, windows):
    feed(names=[f"CouchElephant-Setup-{NEWER}.exe"])
    data = client.get("/api/update").json()
    assert data["how"] == "click" and data["click"] is False and data["tray"] is True
    r = client.post("/api/update/apply")
    assert r.status_code == 400
    assert r.json()["error"] == "Use Update in the CouchElephant icon next to the clock."
    assert windows == []


def test_the_tray_runs_a_checked_installer_with_a_prompt(feed, windows):
    name = f"CouchElephant-Setup-{NEWER}.exe"
    feed(names=[name])
    assert "Updating to" in updates.apply_from_tray()
    path, params = windows[0]
    assert os.path.basename(path) == name
    assert params == "/SILENT /SUPPRESSMSGBOXES /NORESTART"


def test_the_tray_refuses_an_installer_that_does_not_match(feed, windows):
    feed(names=[f"CouchElephant-Setup-{NEWER}.exe"], bad_hash=True)
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.apply_from_tray()
    assert windows == []


def test_the_tray_refuses_when_the_sums_do_not_name_the_file(feed, windows):
    feed(names=[f"CouchElephant-Setup-{NEWER}.exe"])
    _Feed.files["SHA256SUMS"] = b"%s  other-file.exe\n" % (b"0" * 64)
    with pytest.raises(updates.UpdateError, match="no checksum"):
        updates.apply_from_tray()
    assert windows == []


def test_the_tray_offers_the_update_without_the_server(feed, windows):
    feed(names=[f"CouchElephant-Setup-{NEWER}.exe"])
    assert updates.offer() == NEWER
    feed(names=())
    assert updates.offer() is None


def test_the_feed_is_asked_again_after_a_day(feed, monkeypatch):
    feed()
    assert updates.latest().version == NEWER
    feed(_bump(NEWER))
    updates.latest()                       # reset() cleared it; fills the cache
    _Feed.release["tag_name"] = "v" + _bump(NEWER, 2)
    assert updates.latest().version == _bump(NEWER)
    monkeypatch.setattr(updates.time, "time", lambda: 10**10)
    assert updates.latest().version == _bump(NEWER, 2)


# ---------- macOS ----------

class _Mac:
    """Stands in for hdiutil, codesign, spctl and ditto."""
    def __init__(self, signed):
        self.signed, self.calls = signed, []

    def __call__(self, cmd):
        import shutil
        import subprocess
        self.calls.append(cmd[0])
        ok = 0
        if cmd[0] == "hdiutil" and cmd[1] == "attach":
            os.makedirs(os.path.join(cmd[4], "CouchElephant.app"))
        elif cmd[0] in ("codesign", "spctl") and not self.signed:
            ok = 1
        elif cmd[0] == "ditto":
            shutil.copytree(cmd[1], cmd[2])
        return subprocess.CompletedProcess(cmd, ok, "", "")


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_in_docker", lambda: False)
    monkeypatch.setattr(updates, "_system", lambda: "Darwin")
    monkeypatch.setattr(updates, "MAC_APP", str(tmp_path / "CouchElephant.app"))
    spawned = []
    monkeypatch.setattr(updates, "_spawn_detached", lambda cmd: spawned.append(cmd))
    (tmp_path / "CouchElephant.app").mkdir()
    (tmp_path / "CouchElephant.app" / "old").write_text("old")
    return spawned


def _mac_feed(feed, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    feed(names=[f"CouchElephant-{NEWER}-macos-arm64.dmg"])


def test_mac_refuses_an_unsigned_dmg_and_leaves_the_app_alone(feed, mac, monkeypatch, tmp_path):
    _mac_feed(feed, monkeypatch)
    monkeypatch.setattr(updates, "_run", _Mac(signed=False))
    with pytest.raises(updates.UpdateError) as err:
        updates.apply()
    assert str(err.value) == ("This update is not signed yet. "
                              "Download it from couchelephant.app.")
    assert (tmp_path / "CouchElephant.app" / "old").exists()
    assert mac == []


def test_mac_replaces_the_app_and_relaunches_when_signed(feed, mac, monkeypatch, tmp_path):
    _mac_feed(feed, monkeypatch)
    run = _Mac(signed=True)
    monkeypatch.setattr(updates, "_run", run)
    updates.apply()
    assert {"codesign", "spctl"} <= set(run.calls)
    assert (tmp_path / "CouchElephant.app").is_dir()
    assert not (tmp_path / "CouchElephant.app" / "old").exists()
    assert "open -n" in mac[0][-1]


def test_mac_refuses_a_dmg_that_does_not_match(feed, mac, monkeypatch):
    _mac_feed(feed, monkeypatch)
    feed(names=[f"CouchElephant-{NEWER}-macos-arm64.dmg"], bad_hash=True)
    monkeypatch.setattr(updates, "_run", _Mac(signed=True))
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.apply()


# ---------- tray and menu bar ----------

def test_the_tray_offers_the_update_only_when_there_is_one():
    assert [label for label, _ in tray.build_menu(8710)] == ["Open", "Pause recordings", "Quit"]
    assert [label for label, _ in tray.build_menu(8710, "running", "1.6.0")] == \
        ["Open", "Pause recordings", "Update to 1.6.0", "Quit"]


def test_the_menu_bar_offers_the_update_only_when_there_is_one():
    assert [label for label, _ in menubar.build_menu(8710)] == ["Open", "Start at login", "Quit"]
    assert [label for label, _ in menubar.build_menu(8710, "1.6.0")] == \
        ["Open", "Start at login", "Update to 1.6.0", "Quit"]


def test_menu_items_start_the_update(monkeypatch):
    calls = []
    monkeypatch.setattr(updates, "apply_from_tray", lambda: calls.append("tray"))
    monkeypatch.setattr(updates, "apply", lambda: calls.append("mac"))
    dict(tray.build_menu(8710, "running", "1.6.0"))["Update to 1.6.0"]()
    dict(menubar.build_menu(8710, "1.6.0"))["Update to 1.6.0"]()
    assert calls == ["tray", "mac"]
