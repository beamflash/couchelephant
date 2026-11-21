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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import paths
from .version import VERSION

log = logging.getLogger("couchelephant")

DEFAULT_FEED = "https://api.github.com/repos/beamflash/couchelephant/releases/latest"
TIMEOUT = 15
MOVE_FIRST = "Move CouchElephant to Applications first"
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
_apply_lock = threading.Lock()
DAY = 24 * 3600
RETRY = 600                       # a failed check is tried again after 10 minutes
_state: dict = {"at": None, "release": None, "ttl": DAY}

INSTALLER_CAP = 500 * 1024 * 1024
SUMS_CAP = 1024 * 1024
FEED_CAP = 1024 * 1024
DOWNLOAD_SECONDS = 20 * 60
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _check_url(url: str) -> None:
    """https only; plain http is for loopback, which is how the tests run."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        return
    raise UpdateError("The update address is not a secure web address, so it was not used.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, accept: str | None = None):
    _check_url(url)
    headers = {"User-Agent": "CouchElephant"}
    if accept:
        headers["Accept"] = accept
    opener = urllib.request.build_opener(_SafeRedirect)
    return opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)


def _read_capped(response, cap: int, out=None, deadline: float | None = None) -> bytes:
    """Read the whole body, refusing one that is bigger than cap or takes too long."""
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise UpdateError("The download is larger than expected, so it was not used.")
    total, parts = 0, []
    while True:
        chunk = response.read(1 << 16)
        if not chunk:
            return b"".join(parts)
        total += len(chunk)
        if total > cap:
            raise UpdateError("The download is larger than expected, so it was not used.")
        if deadline is not None and time.monotonic() > deadline:
            raise UpdateError("The download took too long, so it was stopped.")
        if out is not None:
            out.write(chunk)
        else:
            parts.append(chunk)


def _fetch(url: str) -> Release | None:
    import json
    with _open(url, "application/vnd.github+json") as r:
        data = json.loads(_read_capped(r, FEED_CAP))
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
        if at is not None and not refresh and time.time() - at < _state["ttl"]:
            return _state["release"]
        _state["at"] = time.time()
        url = _feed_url()
        if not url:
            _state.update(release=None, ttl=DAY)
            return None
        try:
            _state.update(release=_fetch(url), ttl=DAY)
        except Exception as err:
            log.warning("update check failed: %s", err)
            _state.update(release=None, ttl=RETRY)
        return _state["release"]


def available() -> Release | None:
    rel = latest()
    return rel if rel and newer(rel.version, VERSION) else None


def reset() -> None:
    with _lock:
        _state.update(at=None, release=None, ttl=DAY)


def _in_docker() -> bool:
    return paths._in_container()


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

def _download(url: str, dest: str, cap: int | None = None) -> None:
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    with _open(url) as r, open(dest, "wb") as out:
        _read_capped(r, cap or INSTALLER_CAP, out, deadline)


def _expected_hash(sums_url: str, name: str, folder: str) -> str:
    path = os.path.join(folder, "SHA256SUMS")
    _download(sums_url, path, SUMS_CAP)
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            # "<hash>  <name>" or "<hash> *<name>"
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    raise UpdateError("The release has no checksum for this download, so it was not installed.")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_verified(release: Release, name: str) -> tuple[str, str]:
    """Download and check a file. Returns its path and the hash it was checked against."""
    folder = tempfile.mkdtemp(prefix="couchelephant-update-")
    try:
        want = _expected_hash(release.assets["SHA256SUMS"], name, folder)
        dest = os.path.join(folder, name)
        _download(release.assets[name], dest)
        if _sha256(dest) != want:
            raise UpdateError("The download did not match its checksum, so it was not installed.")
        return dest, want
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


def _exclusive(fn):
    def run(*args):
        if not _apply_lock.acquire(blocking=False):
            raise UpdateError("An update is already running.")
        try:
            return fn(*args)
        finally:
            _apply_lock.release()
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


@_exclusive
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


@_exclusive
def apply_from_tray() -> str:
    """Windows: download, check the hash, run the installer with a UAC prompt.

    Called by the tray, which runs in the signed-in user's session.
    """
    rel = available()
    name = asset_name(rel) if rel else None
    if rel is None or name is None or "SHA256SUMS" not in rel.assets:
        raise UpdateError("This release has no installer for this computer yet. "
                          "Download it from couchelephant.app.")
    installer, want = _fetch_verified(rel, name)
    # The file sits in a folder this user can write to. Look again right before
    # the prompt, so a swap between the check and the click is caught.
    if _sha256(installer) != want:
        shutil.rmtree(os.path.dirname(installer), ignore_errors=True)
        raise UpdateError("The download did not match its checksum, so it was not installed.")
    try:
        # The installer stops the service itself and waits for the old program.
        _shell_execute_runas(installer, "/SILENT /SUPPRESSMSGBOXES /NORESTART")
    except OSError as err:
        raise UpdateError(f"The installer was not started: {err}") from err
    return f"Updating to {rel.version}."


NOT_WRITABLE = ("CouchElephant could not be replaced in Applications. Check that this "
                "account may change it, or download the update from couchelephant.app.")


def in_applications(path: str) -> bool:
    exe = os.path.realpath(path)
    roots = (os.path.realpath("/Applications"),
             os.path.realpath(os.path.expanduser("~/Applications")))
    return any(exe.startswith(root + os.sep) for root in roots)


def running_app() -> str:
    """The app bundle this process runs from, the one an update must replace."""
    exe = os.path.realpath(sys.executable)
    parts = exe.split(os.sep)
    for i, part in enumerate(parts):
        if part.endswith(".app"):
            bundle = os.sep.join(parts[:i + 1])
            if in_applications(bundle):
                return bundle
            break
    raise UpdateError(MOVE_FIRST)


def _swap_in(app: str, target: str) -> None:
    """Put the new app in place; on any failure the old one is still there."""
    staged, old = target + ".new", target + ".old"
    try:
        shutil.rmtree(staged, ignore_errors=True)
        if _run(["ditto", app, staged]).returncode != 0:
            raise UpdateError(NOT_WRITABLE)
        shutil.rmtree(old, ignore_errors=True)
        had = os.path.exists(target)
        if had:
            os.rename(target, old)
        try:
            os.rename(staged, target)
        except OSError:
            if had:
                os.rename(old, target)
            raise
        shutil.rmtree(old, ignore_errors=True)
    except OSError as err:
        raise UpdateError(NOT_WRITABLE) from err
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _apply_mac(rel: Release, name: str) -> str:
    target = running_app()          # before any download: a copy outside Applications stops here
    dmg, _ = _fetch_verified(rel, name)
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
        _swap_in(app, target)
    finally:
        _run(["hdiutil", "detach", mount, "-quiet", "-force"])
        shutil.rmtree(folder, ignore_errors=True)
    pid = os.getpid()
    _spawn_detached(["/bin/sh", "-c",
                     f"sleep 1; kill {pid}; n=0; "
                     f"while kill -0 {pid} 2>/dev/null && [ $n -lt 60 ]; do sleep 0.5; n=$((n+1)); done; "
                     f"open -n '{target}'"])
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
        try:
            await asyncio.to_thread(latest, True)
        except Exception as err:       # nothing here may stop the loop
            log.warning("update loop: %s", err)
        await asyncio.sleep(DAY)


# ---------- for the tray and the menu bar ----------

def offer() -> str | None:
    """The version to show as "Update to ...": one this system can install by itself.

    Reads what the last check found. It never touches the network, so a menu
    can call it while it is being drawn.
    """
    rel = _state["release"]
    if rel and newer(rel.version, VERSION) and can_click(rel):
        return rel.version
    return None


def watch(first_after: float = 10, every: float = 60) -> threading.Thread:
    """Check in the background so a menu only ever reads the answer.

    latest() itself decides when a look is due: a day after a good one, ten
    minutes after a failed one.
    """
    def run():
        time.sleep(first_after)
        while True:
            try:
                latest()
            except Exception as err:
                log.warning("update watch: %s", err)
            time.sleep(every)
    thread = threading.Thread(target=run, daemon=True, name="update-watch")
    thread.start()
    return thread


def sweep_stale(older_than: float = DAY) -> None:
    """Remove update folders a crashed or interrupted update left in the temp folder."""
    import glob
    for folder in glob.glob(os.path.join(tempfile.gettempdir(), "couchelephant-update-*")):
        try:
            if time.time() - os.path.getmtime(folder) > older_than:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
