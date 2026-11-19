"""The menu bar icon of the macOS app: Open, Start at login, Quit.

The server runs in a thread of this same process, so quitting the menu
stops everything. The menu is built as data so it can be tested with no
display; rumps is imported only when the app really runs.
"""
import os
import plistlib
import socket
import subprocess
import sys
import threading
import time
import urllib.request
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


class LoginError(Exception):
    """Start at login cannot be set; the message is meant for the user."""


def _in_applications(executable: str) -> bool:
    exe = Path(executable).resolve()
    roots = (Path("/Applications"), Path.home() / "Applications")
    return any(root in exe.parents for root in roots)


def set_login(on: bool, executable: str | None = None) -> None:
    path = login_item_path()
    if not on:
        if path.exists():
            # Unload it too, or it stays loaded until the next login.
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        path.unlink(missing_ok=True)
        return
    executable = executable or sys.executable
    # A copy run from the dmg or Downloads would be a dead path after it is moved.
    if not _in_applications(executable):
        raise LoginError("Move CouchElephant to Applications first")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [executable],
        "RunAtLoad": True,
    }))


PORT_TRIES = 20          # 8710 up to 8729


def free_port(start: int, tries: int = PORT_TRIES) -> int | None:
    """The first port from start that nothing listens on, as the Linux and Windows installers do."""
    for port in range(start, start + tries):
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    return None


class Started:
    def __init__(self, thread, port, error=None):
        self.thread, self.port, self.error = thread, port, error


def start_server(port: int) -> threading.Thread:
    """Run the server in a thread. uvicorn skips signal handlers off the main thread."""
    def target():
        try:
            serve(["--port", str(port), "--open"])
        except BaseException as err:             # uvicorn exits with SystemExit on a bind error
            target.error = err
    target.error = None
    thread = threading.Thread(target=target, daemon=True)
    thread.target = target
    thread.start()
    return thread


def _answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tv/hello", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def bring_up(start: int, wait: float = 60) -> Started:
    """Start the server on the first free port and wait until it answers or dies."""
    port = free_port(start)
    if port is None:
        return Started(None, start, f"ports {start} to {start + PORT_TRIES - 1} are all in use")
    thread = start_server(port)
    deadline = time.time() + wait
    while time.time() < deadline:
        if _answers(port):
            return Started(thread, port)
        if not thread.is_alive():
            err = thread.target.error
            return Started(thread, port, f"the server stopped: {err!r}" if err else "the server stopped")
        time.sleep(0.3)
    return Started(thread, port, f"the server did not answer on port {port} in {wait:.0f} seconds")


def stop_server(thread: threading.Thread) -> None:
    stop_serving()
    thread.join(15)


_server: threading.Thread | None = None


def quit_app() -> None:
    if _server is not None:
        stop_server(_server)
    import rumps
    rumps.quit_application()


def toggle_login() -> None:
    set_login(not login_enabled())


def build_menu(port: int) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("Open", lambda: webbrowser.open(f"http://localhost:{port}/")),
        ("Start at login", toggle_login),
        ("Quit", quit_app),
    ]


def _icon_path() -> str:
    # The spec ships app/static, so this is the same place frozen or not.
    return os.path.join(os.path.dirname(__file__), "static", "icon-64.png")


def run(port: int | None = None) -> None:
    import rumps
    global _server
    started = bring_up(port or paths.port())
    _server = started.thread
    app = rumps.App("CouchElephant", icon=_icon_path(), quit_button=None)
    if started.error:
        # Say why, rather than leave an icon that does nothing.
        rumps.alert("CouchElephant could not start", started.error)
        return

    def login_toggle(item):
        try:
            toggle_login()
        except LoginError as err:
            rumps.alert("Start at login", str(err))
        item.state = login_enabled()

    for label, action in build_menu(started.port):
        if label == "Start at login":
            item = rumps.MenuItem(label, callback=login_toggle)
            item.state = login_enabled()
        else:
            item = rumps.MenuItem(label, callback=lambda _i, action=action: action())
        app.menu.add(item)
    app.run()


if __name__ == "__main__":
    run()
