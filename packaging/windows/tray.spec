# PyInstaller spec for the tray program (Windows only). One folder, no console.
from pathlib import Path

root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(root / "packaging" / "windows" / "tray_launcher.py")],
    pathex=[str(root)],
    datas=[(str(root / "app" / "static" / "icon-64.png"), "static")],
    hiddenimports=["pystray._win32"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CouchElephantTray",
          console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CouchElephantTray")
