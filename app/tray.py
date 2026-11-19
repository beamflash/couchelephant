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

from . import paths, updates

TIMEOUT = 5


def _base(port: int) -> str:
    return f"http://localhost:{port}"


def pause_state(port: int) -> str:
    """"running", "paused", or "locked" when sign-in is on and the tray has no session."""
    import json
    import urllib.error
    try:
        with urllib.request.urlopen(_base(port) + "/api/recorder/status", timeout=TIMEOUT) as r:
            return "paused" if json.load(r).get("paused") else "running"
    except urllib.error.HTTPError as err:
        if err.code == 401:
            return "locked"
        raise


def set_paused(port: int, on: bool) -> None:
    body = urllib.parse.urlencode({"on": "1" if on else "0"}).encode()
    req = urllib.request.Request(_base(port) + "/settings/recorder/pause", data=body)
    urllib.request.urlopen(req, timeout=TIMEOUT).close()


def quit_tray() -> None:
    if _icon is not None:
        _icon.stop()


def apply_update() -> None:
    updates.apply_from_tray()


def build_menu(port: int, state: str = "running",
               update: str | None = None) -> list[tuple[str, Callable[[], None]]]:
    if state == "locked":
        # Signed-in installs: the tray cannot log in, so hand over to the browser.
        pause = ("Pause recordings in Settings",
                 lambda: webbrowser.open(f"http://localhost:{port}/settings"))
    elif state == "paused":
        pause = ("Resume recordings", lambda: set_paused(port, False))
    else:
        pause = ("Pause recordings", lambda: set_paused(port, True))
    menu = [
        ("Open", lambda: webbrowser.open(f"http://localhost:{port}/")),
        pause,
    ]
    if update:
        menu.append((f"Update to {update}", apply_update))
    menu.append(("Quit", quit_tray))
    return menu


_icon = None


def _image():
    from PIL import Image
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(__file__))
    return Image.open(os.path.join(base, "static", "icon-64.png"))


def run(port: int | None = None) -> None:
    global _icon
    import pystray
    port = port or paths.port()
    updates.sweep_stale()
    updates.watch()

    def wrap(action):
        def call(icon, item):
            try:
                action()
            except updates.UpdateError as err:
                icon.notify(str(err), "CouchElephant")
            except Exception as err:     # say so; a silent click looks like a hang
                icon.notify(f"CouchElephant did not answer: {err}", "CouchElephant")
        return call

    def items():
        try:
            state = pause_state(port)
        except Exception:
            state = "running"
        for label, action in build_menu(port, state, updates.offer()):
            yield pystray.MenuItem(label, wrap(action), default=(label == "Open"))

    _icon = pystray.Icon("CouchElephant", _image(), "CouchElephant", pystray.Menu(items))
    _icon.run()


if __name__ == "__main__":
    run()
