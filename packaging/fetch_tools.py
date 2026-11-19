"""Download the pinned ffmpeg, ffprobe (and comskip on Windows) into <dest>/bin.

    python packaging/fetch_tools.py <windows|macos|linux> <x64|arm64> <dest>

Every file is checked against the SHA-256 in tools.json before it is opened.
"""
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


TRIES = 3


def _download(url: str, sha256: str, target: Path) -> None:
    for attempt in range(1, TRIES + 1):
        digest = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "couchelephant-build"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as out:
                while chunk := resp.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
            break
        except OSError as err:           # a slow mirror; a bad hash is not retried
            if attempt == TRIES:
                raise
            print(f"download failed ({err!r}), try {attempt + 1} of {TRIES}: {url}", file=sys.stderr)
            time.sleep(5 * attempt)
    if digest.hexdigest() != sha256:
        raise SystemExit(f"hash mismatch for {url}\n  want {sha256}\n  got  {digest.hexdigest()}")


def _members(archive: Path):
    """Yield (name, opener) for each file in a zip or tar archive."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not info.is_dir():
                    yield info.filename, (lambda i=info: z.open(i))
    else:
        with tarfile.open(archive) as t:
            for info in t:
                if info.isfile():
                    yield info.name, (lambda i=info: t.extractfile(i))


def _unpack(archive: Path, pick: dict, bin_dir: Path) -> None:
    left = dict(pick)
    for name, opener in _members(archive):
        for suffix, out_name in list(left.items()):
            if name == suffix.lstrip("/") or name.endswith(suffix if suffix.startswith("/") else "/" + suffix):
                with opener() as src, open(bin_dir / out_name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if not out_name.endswith(".txt"):
                    (bin_dir / out_name).chmod(0o755)
                del left[suffix]
                break
    if left:
        raise SystemExit(f"not found in {archive.name}: {', '.join(left)}")


def fetch(system: str, arch: str, dest: Path) -> None:
    entries = json.loads((HERE / "tools.json").read_text())
    key = f"{system}-{arch}"
    if key not in entries:
        raise SystemExit(f"no entry for {key}")
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for i, item in enumerate(entries[key]["downloads"]):
            archive = Path(tmp) / f"download{i}"
            _download(item["url"], item["sha256"], archive)
            _unpack(archive, item["pick"], bin_dir)
    print(f"{key}: {', '.join(sorted(p.name for p in bin_dir.iterdir()))}")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("windows", "macos", "linux") \
            or sys.argv[2] not in ("x64", "arm64"):
        raise SystemExit(__doc__)
    fetch(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
