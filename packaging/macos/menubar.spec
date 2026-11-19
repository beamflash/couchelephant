# PyInstaller spec for CouchElephant.app: the menu bar app with the server inside.
# Fetch the programs first (fetch_tools.py macos <arch> and build_comskip.sh), then
#   COUCHELEPHANT_TOOLS=<dir> pyinstaller packaging/macos/menubar.spec
import os
from pathlib import Path

root = Path(SPECPATH).parent.parent
tools_bin = Path(os.environ.get("COUCHELEPHANT_TOOLS", root / "packaging" / "tools")) / "bin"
version = [line for line in (root / "app" / "version.py").read_text().splitlines()
           if line.startswith("VERSION = ")][0].split('"')[1]

hidden = [
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "app.web",
]

binaries = [(str(tools_bin / n), "bin") for n in ("ffmpeg", "ffprobe", "comskip")]
missing = [src for src, _ in binaries if not Path(src).is_file()]
if missing:
    raise SystemExit("missing tools, run fetch_tools.py / build_comskip.sh first: " + ", ".join(missing))
licences = [(str(p), "bin") for p in (root / "packaging" / "licenses").glob("LICENSE-*.txt")]

a = Analysis(
    [str(root / "packaging" / "macos" / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=[
        (str(root / "app" / "templates"), "app/templates"),
        (str(root / "app" / "static"), "app/static"),
        (str(root / "app" / "data"), "app/data"),
    ] + licences,
    hiddenimports=hidden,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CouchElephant", console=False,
          entitlements_file=str(root / "packaging" / "macos" / "entitlements.plist"))
coll = COLLECT(exe, a.binaries, a.datas, name="CouchElephant")
app = BUNDLE(
    coll,
    name="CouchElephant.app",
    icon=None,
    bundle_identifier="app.couchelephant",
    version=version,
    info_plist={
        "LSUIElement": True,                  # menu bar only, no Dock icon
        "CFBundleDisplayName": "CouchElephant",
        "CFBundleShortVersionString": version,
        "NSHighResolutionCapable": True,
    },
)
