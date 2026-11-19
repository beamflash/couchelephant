import os

import pytest

from app import paths


def test_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path / "with space"))
    assert paths.data_dir() == tmp_path / "with space"
    assert paths.data_dir().is_dir()          # made on first use


def test_docker_keeps_data(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_DATA", raising=False)
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(paths.Path, "mkdir", lambda *a, **k: None)
    assert str(paths.data_dir()) == "/data"


@pytest.mark.parametrize("system,expect", [
    ("Windows", "CouchElephant"), ("Darwin", "Application Support/CouchElephant"),
    ("Linux", "/var/lib/couchelephant")])
def test_native_defaults(monkeypatch, system, expect):
    monkeypatch.delenv("COUCHELEPHANT_DATA", raising=False)
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(paths.platform, "system", lambda: system)
    monkeypatch.setattr(paths.os, "access", lambda *a: True)
    monkeypatch.setattr(paths.Path, "mkdir", lambda *a, **k: None)
    assert expect in paths.data_dir().as_posix()


def test_linux_falls_back_when_not_writable(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_DATA", raising=False)
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(paths.platform, "system", lambda: "Linux")
    monkeypatch.setattr(paths.os, "access", lambda *a: False)
    monkeypatch.setattr(paths.Path, "mkdir", lambda *a, **k: None)
    assert paths.data_dir().as_posix().endswith(".local/share/couchelephant")


def test_native_recordings_under_data(monkeypatch, tmp_path):
    monkeypatch.delenv("COUCHELEPHANT_RECORDINGS", raising=False)
    monkeypatch.delenv("COUCHELEPHANT_LIBRARY", raising=False)
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path))
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    assert paths.recordings_dir() == tmp_path / "recordings"
    assert not (tmp_path / "recordings").exists()   # the installers make it
    assert paths.library_dir() is None


def test_container_folders(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_RECORDINGS", raising=False)
    monkeypatch.delenv("COUCHELEPHANT_LIBRARY", raising=False)
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert str(paths.recordings_dir()) == "/recordings"
    assert str(paths.library_dir()) == "/library"


def test_port_default_and_env(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_PORT", raising=False)
    assert paths.port() == 8710
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8711")
    assert paths.port() == 8711


def test_tool_prefers_bundled(monkeypatch, tmp_path):
    monkeypatch.delenv("COUCHELEPHANT_FFMPEG", raising=False)
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setattr(paths, "_bundle_root", lambda: tmp_path)
    assert paths.tool("ffmpeg") == str(exe)


def test_tool_env_then_path(monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_FFMPEG", "/opt/x/ffmpeg")
    assert paths.tool("ffmpeg") == "/opt/x/ffmpeg"
    monkeypatch.delenv("COUCHELEPHANT_FFMPEG")
    assert paths.tool("ffmpeg") == "ffmpeg"


def test_install_id_is_stable():
    assert paths.install_id() == paths.install_id()
    assert len(paths.install_id()) == 32
