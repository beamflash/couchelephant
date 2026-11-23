"""Runs the Firestore rules test in the emulator. Skips when the tools or
Java are missing, so a bare checkout still passes."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "tests" / "firestore_rules"


def test_firestore_rules():
    for tool in ("firebase", "node", "npm"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    if not shutil.which("java"):
        pytest.skip("the Firestore emulator needs Java, which is not installed")
    def run(*a, cwd=DIR):
        return subprocess.run(a, cwd=cwd, capture_output=True, text=True,
                              timeout=600)
    if not (DIR / "node_modules").is_dir():
        inst = run("npm", "ci" if (DIR / "package-lock.json").exists() else "install")
        assert inst.returncode == 0, inst.stdout + inst.stderr
    r = run("firebase", "emulators:exec", "--only", "firestore",
            "--project", "ce-rules-test", "npm --prefix tests/firestore_rules test",
            cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
