"""The one place that decides where things live.

Docker keeps /data, /recordings and /library. A native install (the
installers) uses a per-system data folder and the programs it ships. Every
setting still wins when it is set, so nothing about Docker changes.
"""
import os
import platform
import sys
import uuid
from pathlib import Path

DEFAULT_PORT = 8710


def _in_container() -> bool:
    return (os.path.exists("/.dockerenv")
            or os.environ.get("COUCHELEPHANT_DB", "").startswith("/data/"))


def _bundle_root():
    """Where a frozen build unpacks its files, or None when run from source."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
    return None


def _native_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CouchElephant"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "CouchElephant"
    system_dir = Path("/var/lib/couchelephant")
    probe = system_dir if system_dir.exists() else system_dir.parent
    if os.access(probe, os.W_OK):
        return system_dir
    # A tarball run by hand, not as the service user.
    return Path.home() / ".local" / "share" / "couchelephant"


def data_dir() -> Path:
    env = os.environ.get("COUCHELEPHANT_DATA")
    if env:
        path = Path(env)
    elif _in_container():
        path = Path("/data")
    else:
        path = _native_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> str:
    return (os.environ.get("COUCHELEPHANT_DB")
            or str(data_dir() / "couchelephant.db"))


def auth_db_path() -> str:
    return (os.environ.get("COUCHELEPHANT_AUTH_DB")
            or os.path.join(os.path.dirname(db_path()), "auth.db"))


def logos_dir() -> str:
    return os.environ.get("COUCHELEPHANT_LOGOS") or str(data_dir() / "logos")


def port() -> int:
    try:
        return int(os.environ.get("COUCHELEPHANT_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def tool(name: str) -> str:
    """ffmpeg, ffprobe or comskip: the env override, the bundled copy, then PATH."""
    env = os.environ.get(f"COUCHELEPHANT_{name.upper()}")
    if env:
        return env
    root = _bundle_root()
    if root is not None:
        exe = Path(root) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if exe.is_file():
            return str(exe)
    return name


def recordings_dir() -> Path:
    # Not made here; the installers create the native folder.
    env = os.environ.get("COUCHELEPHANT_RECORDINGS")
    if env:
        return Path(env)
    if _in_container():
        return Path("/recordings")
    return data_dir() / "recordings"


def library_dir() -> Path | None:
    """None on a native install: the folders are then absolute paths."""
    env = os.environ.get("COUCHELEPHANT_LIBRARY")
    if env:
        return Path(env)
    return Path("/library") if _in_container() else None


def install_id() -> str:
    from . import db  # db imports this module
    value = db.get_setting("install_id")
    if not value:
        value = uuid.uuid4().hex
        db.set_setting("install_id", value)
    return value
