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


def test_the_login_toggle_flips_the_state():
    toggle = menubar.build_menu(8710)[1][1]
    toggle()
    assert menubar.login_enabled() is True
    toggle()
    assert menubar.login_enabled() is False


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
