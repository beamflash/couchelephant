"""The tray menu is plain data, so it is tested with no display and no pystray."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from app import tray


def test_tray_menu():
    menu = tray.build_menu(8712)
    assert [label for label, _ in menu] == ["Open", "Pause recordings", "Quit"]
    with mock.patch("webbrowser.open") as opened:
        menu[0][1]()
    opened.assert_called_once_with("http://localhost:8712/")


def test_the_menu_offers_resume_while_paused():
    assert [label for label, _ in tray.build_menu(8710, paused=True)] == \
        ["Open", "Resume recordings", "Quit"]


class _Server(BaseHTTPRequestHandler):
    seen: list = []
    paused = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        type(self).seen.append((self.path, body))
        type(self).paused = body == "on=1"
        self._send({"ok": True, "paused": type(self).paused})

    def do_GET(self):
        self._send({"ok": True, "paused": type(self).paused})

    def _send(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def test_pause_and_resume_post_to_the_recorder_setting():
    _Server.seen, _Server.paused = [], False
    srv = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert tray.is_paused(port) is False
        tray.build_menu(port)[1][1]()
        assert tray.is_paused(port) is True
        tray.build_menu(port, paused=True)[1][1]()
        assert tray.is_paused(port) is False
    finally:
        srv.shutdown()
    assert _Server.seen == [("/settings/recorder/pause", "on=1"),
                            ("/settings/recorder/pause", "on=0")]


def test_importing_the_tray_does_not_need_pystray():
    import sys
    assert "pystray" not in sys.modules
