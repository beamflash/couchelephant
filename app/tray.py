"""The notification-area icon of the Windows install: Open, Pause, Quit.

The menu is built as data so it can be tested with no display. pystray is
imported only when the icon really runs.
"""
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable

from . import paths

TIMEOUT = 5


def _base(port: int) -> str:
    return f"http://localhost:{port}"


def is_paused(port: int) -> bool:
    import json
    with urllib.request.urlopen(_base(port) + "/api/recorder/status", timeout=TIMEOUT) as r:
        return bool(json.load(r).get("paused"))


def set_paused(port: int, on: bool) -> None:
    body = urllib.parse.urlencode({"on": "1" if on else "0"}).encode()
    req = urllib.request.Request(_base(port) + "/settings/recorder/pause", data=body)
    urllib.request.urlopen(req, timeout=TIMEOUT).close()


def quit_tray() -> None:
    if _icon is not None:
        _icon.stop()


def build_menu(port: int, paused: bool = False) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("Open", lambda: webbrowser.open(f"http://localhost:{port}/")),
        ("Resume recordings" if paused else "Pause recordings",
         lambda: set_paused(port, not paused)),
        ("Quit", quit_tray),
    ]


_icon = None


def _image():
    from PIL import Image
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(__file__))
    return Image.open(os.path.join(base, "static", "icon-64.png"))


def run(port: int | None = None) -> None:
    global _icon
    import pystray
    port = port or paths.port()

    def wrap(action):
        def call(icon, item):
            try:
                action()
            except Exception as err:     # say so; a silent click looks like a hang
                icon.notify(f"CouchElephant did not answer: {err}", "CouchElephant")
        return call

    def items():
        try:
            paused = is_paused(port)
        except Exception:
            paused = False
        for label, action in build_menu(port, paused):
            yield pystray.MenuItem(label, wrap(action), default=(label == "Open"))

    _icon = pystray.Icon("CouchElephant", _image(), "CouchElephant", pystray.Menu(items))
    _icon.run()


if __name__ == "__main__":
    run()
