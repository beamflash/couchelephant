"""python -m app: runs the server, and opens the browser only on first run."""
import socket
import threading
import time
import urllib.request
from unittest import mock

from app import __main__ as entry
from app import db


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hello(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tv/hello", timeout=2) as r:
            return r.status
    except OSError:
        return None


def test_main_starts_and_answers_hello(tmp_path, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path))
    port = _free_port()
    result = []
    t = threading.Thread(
        target=lambda: result.append(entry.main(["--port", str(port), "--no-open"])),
        daemon=True)
    t.start()
    try:
        deadline = time.time() + 20
        status = None
        while time.time() < deadline and status != 200:
            status = _hello(port)
            time.sleep(0.2)
        assert status == 200
    finally:
        entry.stop()
        t.join(15)
    assert result == [0]


def _run_open(port, monkeypatch, tmp_path):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path))
    monkeypatch.setattr(entry, "OPEN_DELAY", 0)
    with mock.patch("webbrowser.open") as opened, \
         mock.patch("uvicorn.Server.run", lambda self: time.sleep(0.5)):
        assert entry.main(["--port", str(port), "--open"]) == 0
        time.sleep(0.3)
    return opened


def test_open_only_on_first_run(tmp_path, monkeypatch):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    opened = _run_open(8123, monkeypatch, tmp_path)
    opened.assert_called_once_with("http://localhost:8123/welcome")

    db.init()
    db.set_setting("plex_url", "http://plex.invalid:32400")
    db.set_setting("plex_token", "x")
    opened = _run_open(8123, monkeypatch, tmp_path)
    opened.assert_not_called()


def test_no_open_flag_never_opens(tmp_path, monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path))
    with mock.patch("webbrowser.open") as opened, \
         mock.patch("uvicorn.Server.run", lambda self: None):
        entry.main(["--port", "8124", "--no-open"])
        time.sleep(0.2)
    opened.assert_not_called()
