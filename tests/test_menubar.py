"""The macOS menu is plain data, so it is tested with no display and no rumps."""
import plistlib
import socket
import time
import urllib.request
from unittest import mock

import pytest

from app import menubar


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path / "data"))


def test_login_item_path_is_the_launch_agent():
    path = menubar.login_item_path()
    assert path.name == "app.couchelephant.plist"
    assert path.parent.name == "LaunchAgents"
    assert path.parent.parent.name == "Library"


def test_menu_labels_and_open():
    menu = menubar.build_menu(8712)
    assert [label for label, _ in menu] == ["Open", "Start at login", "Quit"]
    with mock.patch("webbrowser.open") as opened:
        menu[0][1]()
    opened.assert_called_once_with("http://localhost:8712/")


def test_start_at_login_writes_then_removes_the_plist():
    assert menubar.login_enabled() is False
    menubar.set_login(True, "/Applications/CouchElephant.app/Contents/MacOS/CouchElephant")
    assert menubar.login_enabled() is True
    plist = plistlib.loads(menubar.login_item_path().read_bytes())
    assert plist["ProgramArguments"] == [
        "/Applications/CouchElephant.app/Contents/MacOS/CouchElephant"]
    assert plist["RunAtLoad"] is True
    assert plist["Label"] == "app.couchelephant"
    menubar.set_login(False)
    assert menubar.login_enabled() is False
    menubar.set_login(False)            # turning it off twice is fine


def test_the_login_toggle_flips_the_state(monkeypatch):
    monkeypatch.setattr(menubar.sys, "executable", "/Applications/CouchElephant.app/x")
    toggle = menubar.build_menu(8710)[1][1]
    toggle()
    assert menubar.login_enabled() is True
    toggle()
    assert menubar.login_enabled() is False


@pytest.mark.parametrize("where,ok", [
    ("/Applications/CouchElephant.app/Contents/MacOS/CouchElephant", True),
    ("~/Applications/CouchElephant.app/Contents/MacOS/CouchElephant", True),
    ("/Volumes/CouchElephant/CouchElephant.app/Contents/MacOS/CouchElephant", False),
    ("~/Downloads/CouchElephant.app/Contents/MacOS/CouchElephant", False),
    ("/tmp/Applications/CouchElephant.app/x", False),
])
def test_start_at_login_only_from_applications(where, ok):
    exe = where.replace("~", str(menubar.Path.home()))
    if ok:
        menubar.set_login(True, exe)
        assert menubar.login_enabled()
    else:
        with pytest.raises(menubar.LoginError, match="Move CouchElephant to Applications first"):
            menubar.set_login(True, exe)
        assert not menubar.login_enabled()


def test_turning_login_off_unloads_a_loaded_agent():
    menubar.set_login(True, "/Applications/CouchElephant.app/x")
    with mock.patch("subprocess.run") as run:
        menubar.set_login(False)
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["launchctl", "bootout"] and cmd[2].endswith("/app.couchelephant")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        menubar.set_login(True, "/Applications/CouchElephant.app/x")
        menubar.set_login(False)             # no launchctl is not an error
    assert not menubar.login_enabled()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_runs_in_a_thread_and_stops():
    port = _free_port()
    thread = menubar.start_server(port)
    try:
        deadline, status = time.time() + 20, None
        while time.time() < deadline and status != 200:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/tv/hello", timeout=2) as r:
                    status = r.status
            except OSError:
                time.sleep(0.2)
        assert status == 200
        assert thread.is_alive()
    finally:
        menubar.stop_server(thread)
    assert not thread.is_alive()


def _hello(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tv/hello", timeout=2) as r:
            return r.status
    except OSError:
        return None


def test_a_taken_port_moves_the_server_up_and_open_follows():
    with socket.socket() as hog:
        hog.bind(("0.0.0.0", 0))
        taken = hog.getsockname()[1]
        started = menubar.bring_up(taken)
        try:
            assert started.error is None
            assert started.port != taken and started.port > taken
            assert _hello(started.port) == 200
            with mock.patch("webbrowser.open") as opened:
                menubar.build_menu(started.port)[0][1]()
            opened.assert_called_once_with(f"http://localhost:{started.port}/")
        finally:
            menubar.stop_server(started.thread)


def test_no_free_port_is_reported_not_swallowed():
    with mock.patch.object(menubar, "free_port", return_value=None):
        started = menubar.bring_up(8710)
    assert started.thread is None and "in use" in started.error


def test_a_server_that_dies_is_reported():
    with mock.patch.object(menubar, "serve", side_effect=RuntimeError("boom")):
        started = menubar.bring_up(_free_port(), wait=10)
    assert "boom" in started.error
