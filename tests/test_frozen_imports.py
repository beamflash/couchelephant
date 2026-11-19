"""The frozen tray ships only app/*.py it imports, no templates or static.
Anything that touches those, or the locked data folder, at import time would
crash it at start."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["app.tray", "app.updates"])
def test_tray_side_imports_stay_light(module):
    code = (
        f"import sys, {module}\n"
        "bad = [m for m in ('app.routes._shared', 'app.db', 'app.auth', 'app.recorder',"
        " 'app.web') if m in sys.modules]\n"
        "print(','.join(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "", f"{module} pulled in {out}"


def test_version_has_no_side_effects_and_is_shared():
    from app import version
    from app.routes import _shared
    assert _shared.VERSION == version.VERSION
