"""Start a frozen build and check it works.

    python packaging/smoke.py <path-to-executable>

Exits 0 and prints "smoke ok" on success.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> int:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status


def _wait_for(url: str, proc: subprocess.Popen, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"server exited early with {proc.returncode}")
        try:
            if _get(url) == 200:
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"no answer from {url} in {seconds}s")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise SystemExit("server did not stop on terminate") from None


def main(exe_arg: str) -> int:
    exe = Path(exe_arg).resolve()
    if not exe.is_file():
        raise SystemExit(f"no such file: {exe}")
    suffix = ".exe" if os.name == "nt" else ""
    bin_dir = exe.parent / "bin"
    with tempfile.TemporaryDirectory() as data:
        port = _free_port()
        env = dict(os.environ, COUCHELEPHANT_DATA=data)
        for key in ("COUCHELEPHANT_DB", "COUCHELEPHANT_FFMPEG",
                    "COUCHELEPHANT_FFPROBE", "COUCHELEPHANT_COMSKIP"):
            env.pop(key, None)       # the build must use what is inside it
        proc = subprocess.Popen([str(exe), "--port", str(port), "--no-open"], env=env)
        try:
            base = f"http://127.0.0.1:{port}"
            _wait_for(f"{base}/api/tv/hello", proc)
            if _get(f"{base}/static/css/app.css") != 200:
                raise SystemExit("static files are missing from the build")
        finally:
            _stop(proc)
        if not (Path(data) / "couchelephant.db").is_file():
            raise SystemExit("couchelephant.db was not made in the data folder")
    for name in ("ffmpeg", "ffprobe"):
        done = subprocess.run([str(bin_dir / (name + suffix)), "-version"],
                              capture_output=True, timeout=60)
        if done.returncode != 0:
            raise SystemExit(f"{name} -version failed")
    # comskip has no clean --help exit; it only has to start.
    subprocess.run([str(bin_dir / ("comskip" + suffix)), "--help"],
                   capture_output=True, timeout=60)
    print("smoke ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
