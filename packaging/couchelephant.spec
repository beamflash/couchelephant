# PyInstaller spec: one folder, with ffmpeg, ffprobe and comskip in bin/.
# Fetch the programs first:  python packaging/fetch_tools.py <os> <arch> packaging/tools
# (and packaging/build_comskip.sh packaging/tools on Linux and macOS).
import os
import sys
from pathlib import Path

root = Path(SPECPATH).parent
tools_bin = Path(os.environ.get("COUCHELEPHANT_TOOLS", root / "packaging" / "tools")) / "bin"

# uvicorn picks these by name at run time, so the analysis cannot see them.
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

wanted = ("ffmpeg", "ffprobe", "comskip")
exe_suffix = ".exe" if sys.platform == "win32" else ""
binaries = [(str(tools_bin / (n + exe_suffix)), "bin") for n in wanted]
missing = [src for src, _ in binaries if not Path(src).is_file()]
if missing:
    raise SystemExit("missing tools, run fetch_tools.py / build_comskip.sh first: " + ", ".join(missing))
licences = [(str(p), "bin") for p in tools_bin.glob("LICENSE*")]

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
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
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    contents_directory=".",           # so bin/ sits beside the executable
    name="couchelephant",
    console=sys.platform != "win32",   # the Windows service has no console to show
)
coll = COLLECT(exe, a.binaries, a.datas, name="couchelephant")
