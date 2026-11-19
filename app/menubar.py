"""The menu bar icon of the macOS app: Open, Start at login, Quit.

The server runs in a thread of this same process, so quitting the menu
stops everything. The menu is built as data so it can be tested with no
display; rumps is imported only when the app really runs.
"""
import os
import plistlib
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from . import paths
from .__main__ import main as serve  # this form is the one PyInstaller can see
from .__main__ import stop as stop_serving

LABEL = "app.couchelephant"


def login_item_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def login_enabled() -> bool:
    return login_item_path().exists()


def set_login(on: bool, executable: str | None = None) -> None:
    path = login_item_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # The frozen app's own executable, so login starts exactly what is running now.
    path.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [executable or sys.executable],
        "RunAtLoad": True,
    }))


def start_server(port: int) -> threading.Thread:
    """Run the server in a thread. uvicorn skips signal handlers off the main thread."""
    thread = threading.Thread(
        target=serve, args=(["--port", str(port), "--open"],), daemon=True)
    thread.start()
    return thread


def stop_server(thread: threading.Thread) -> None:
    stop_serving()
    thread.join(15)


_server: threading.Thread | None = None


def quit_app() -> None:
    if _server is not None:
        stop_server(_server)
    import rumps
    rumps.quit_application()


def build_menu(port: int) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("Open", lambda: webbrowser.open(f"http://localhost:{port}/")),
        ("Start at login", lambda: set_login(not login_enabled())),
        ("Quit", quit_app),
    ]


def _icon_path() -> str:
    # The spec ships app/static, so this is the same place frozen or not.
    return os.path.join(os.path.dirname(__file__), "static", "icon-64.png")


def run(port: int | None = None) -> None:
    import rumps
    global _server
    port = port or paths.port()
    _server = start_server(port)

    app = rumps.App("CouchElephant", icon=_icon_path(), quit_button=None)
    for label, action in build_menu(port):
        if label == "Start at login":
            def toggle(item, action=action):
                action()
                item.state = login_enabled()
            item = rumps.MenuItem(label, callback=toggle)
            item.state = login_enabled()
        else:
            item = rumps.MenuItem(label, callback=lambda _i, action=action: action())
        app.menu.add(item)
    app.run()


if __name__ == "__main__":
    run()
