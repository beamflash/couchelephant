"""Notice a newer release, and on Windows and macOS install it.

The feed is the GitHub releases API, asked once a day. Nothing here may stop
the server: a feed that is down just means no banner today.
"""
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from . import paths
from .routes._shared import VERSION

log = logging.getLogger("couchelephant")

DEFAULT_FEED = "https://api.github.com/repos/datbird/couchelephant/releases/latest"
TIMEOUT = 15
MAC_APP = "/Applications/CouchElephant.app"
UNSIGNED = "This update is not signed yet. Download it from couchelephant.app."
TRAY_LINE = "Use Update in the CouchElephant icon next to the clock."
LINUX_COMMAND = "curl -fsSL https://couchelephant.app/install.sh | sudo sh"


@dataclass
class Release:
    version: str
    url: str
    assets: dict[str, str] = field(default_factory=dict)    # file name -> download URL


class UpdateError(Exception):
    """Meant for the person who pressed the button."""


def _feed_url() -> str:
    # Set but empty means updates are off (the test suite does this).
    return os.environ.get("COUCHELEPHANT_RELEASES_URL", DEFAULT_FEED)


def _numbers(version: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def newer(candidate: str, current: str) -> bool:
    a, b = _numbers(candidate), _numbers(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    pad = lambda t: t + (0,) * (width - len(t))      # noqa: E731
    return pad(a) > pad(b)


_lock = threading.Lock()
DAY = 24 * 3600
_state: dict = {"at": None, "release": None}


def _fetch(url: str) -> Release | None:
    import json
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "CouchElephant"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    if data.get("draft") or data.get("prerelease"):
        return None
    version = str(data.get("tag_name") or "").lstrip("v")
    if _numbers(version) is None:
        return None
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])
              if a.get("name") and a.get("browser_download_url")}
    return Release(version, data.get("html_url") or "", assets)


def latest(refresh: bool = False) -> Release | None:
    """The newest published release, from the last look. Looks once if it never has."""
    with _lock:
        at = _state["at"]
        if at is not None and not refresh and time.time() - at < DAY:
            return _state["release"]
        _state["at"] = time.time()
        url = _feed_url()
        if not url:
            _state["release"] = None
            return None
        try:
            _state["release"] = _fetch(url)
        except Exception as err:
            log.warning("update check failed: %s", err)
            _state["release"] = None
        return _state["release"]


def available() -> Release | None:
    rel = latest()
    return rel if rel and newer(rel.version, VERSION) else None


def reset() -> None:
    with _lock:
        _state.update(at=None, release=None)


def _in_docker() -> bool:
    return paths.library_dir() is not None


def _system() -> str:
    return platform.system()


def how() -> str:
    if _in_docker():
        return "docker"
    return "click" if _system() in ("Windows", "Darwin") else "command"


def asset_name(release: Release) -> str | None:
    """The file for this system, or None when the release does not carry one."""
    if how() != "click":
        return None
    if _system() == "Windows":
        names = (f"CouchElephant-Setup-{release.version}.exe",
                 f"CouchElephant-{release.version}-windows-x64-setup.exe")
    else:
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        names = (f"CouchElephant-{release.version}-macos-{arch}.dmg",)
    return next((n for n in names if n in release.assets), None)


def can_click(release: Release) -> bool:
    return asset_name(release) is not None and "SHA256SUMS" in release.assets


# ---------- applying ----------

def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "CouchElephant"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as out:
        shutil.copyfileobj(r, out)


def _expected_hash(sums_url: str, name: str, folder: str) -> str:
    path = os.path.join(folder, "SHA256SUMS")
    _download(sums_url, path)
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            # "<hash>  <name>" or "<hash> *<name>"
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    raise UpdateError("The release has no checksum for this download, so it was not installed.")


def _fetch_verified(release: Release, name: str) -> str:
    folder = tempfile.mkdtemp(prefix="couchelephant-update-")
    try:
        want = _expected_hash(release.assets["SHA256SUMS"], name, folder)
        dest = os.path.join(folder, name)
        _download(release.assets[name], dest)
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise UpdateError("The download did not match its checksum, so it was not installed.")
        return dest
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _spawn_detached(cmd: list[str]) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        # It has to outlive this service, which the installer stops.
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def apply() -> str:
    """Install the newer release. Returns a line to show; raises UpdateError."""
    mode = how()
    if mode == "docker":
        raise UpdateError("Update the container: pull the new image and recreate it.")
    if mode == "command":
        raise UpdateError(f"Update from a terminal: {LINUX_COMMAND}")
    rel = available()
    if rel is None:
        raise UpdateError("You are up to date.")
    name = asset_name(rel)
    if name is None or "SHA256SUMS" not in rel.assets:
        raise UpdateError("This release has no installer for this computer yet. "
                          "Download it from couchelephant.app.")
    if _system() == "Windows":
        # The service is LocalService and cannot show the UAC prompt the
        # installer needs. The tray runs in the user's session and can.
        raise UpdateError(TRAY_LINE)
    return _apply_mac(rel, name)


def _shell_execute_runas(path: str, params: str) -> None:
    import ctypes
    # Above 32 means started; anything else is an error code (5 = refused at UAC).
    code = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, params, None, 1)
    if code <= 32:
        raise OSError(f"ShellExecute returned {code}")


def apply_from_tray() -> str:
    """Windows: download, check the hash, run the installer with a UAC prompt.

    Called by the tray, which runs in the signed-in user's session.
    """
    rel = available()
    name = asset_name(rel) if rel else None
    if rel is None or name is None or "SHA256SUMS" not in rel.assets:
        raise UpdateError("This release has no installer for this computer yet. "
                          "Download it from couchelephant.app.")
    installer = _fetch_verified(rel, name)
    try:
        # The installer stops the service itself and waits for the old program.
        _shell_execute_runas(installer, "/SILENT /SUPPRESSMSGBOXES /NORESTART")
    except OSError as err:
        raise UpdateError(f"The installer was not started: {err}") from err
    return f"Updating to {rel.version}."


def _apply_mac(rel: Release, name: str) -> str:
    dmg = _fetch_verified(rel, name)
    folder = os.path.dirname(dmg)
    mount = os.path.join(folder, "mnt")
    os.makedirs(mount)
    attach = _run(["hdiutil", "attach", dmg, "-mountpoint", mount,
                   "-nobrowse", "-readonly", "-quiet"])
    if attach.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError("The update could not be opened.")
    try:
        app = os.path.join(mount, "CouchElephant.app")
        # Never skipped: an app that fails either check is not installed.
        if (_run(["codesign", "--verify", "--deep", "--strict", app]).returncode != 0
                or _run(["spctl", "--assess", "--type", "execute", app]).returncode != 0):
            raise UpdateError(UNSIGNED)
        staged = MAC_APP + ".new"
        shutil.rmtree(staged, ignore_errors=True)
        if _run(["ditto", app, staged]).returncode != 0:
            raise UpdateError("The update could not be copied to Applications.")
        old = MAC_APP + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(MAC_APP):
            os.rename(MAC_APP, old)
        os.rename(staged, MAC_APP)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        _run(["hdiutil", "detach", mount, "-quiet", "-force"])
        shutil.rmtree(folder, ignore_errors=True)
    pid = os.getpid()
    _spawn_detached(["/bin/sh", "-c",
                     f"sleep 1; kill {pid}; while kill -0 {pid} 2>/dev/null; do sleep 0.5; done; "
                     f"open -n '{MAC_APP}'"])
    return f"Updating to {rel.version}. CouchElephant will reopen in a moment."


def status() -> dict:
    rel = available()
    return {"ok": True, "current": VERSION, "latest": rel.version if rel else None,
            "how": how(), "url": rel.url if rel else None,
            "click": bool(rel and can_click(rel) and _system() != "Windows"),
            "tray": bool(rel and can_click(rel) and _system() == "Windows")}



async def update_loop():
    import asyncio
    await asyncio.sleep(30)
    while True:
        await asyncio.to_thread(latest, True)
        await asyncio.sleep(24 * 3600)




# ---------- for the tray and the menu bar ----------

def offer() -> str | None:
    """The version to show as "Update to ...": one this system can install by itself."""
    rel = available()
    return rel.version if rel and can_click(rel) else None
