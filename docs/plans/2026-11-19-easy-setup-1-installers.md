# Easy setup, part 1: one download per system - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CouchElephant runs from one download on Windows, macOS and Linux with nothing to install first, and the download page at couchelephant.app offers the right one.

**Architecture:** The server keeps one code base. A new `app/paths.py` decides the data folder, the port and the bundled programs per system, so Docker keeps working through its environment variables. PyInstaller freezes the server (one folder) with ffmpeg, ffprobe and comskip beside it. Thin per-system wrappers install it as a service: an Inno Setup installer plus WinSW on Windows, a menu bar `.app` in a `.dmg` on macOS, nfpm `.deb`/`.rpm` plus a tarball and `install.sh` on Linux. GitHub Actions builds and smoke-tests all of it on a version tag. A static site on Cloudflare Pages serves couchelephant.app.

**Tech Stack:** Python 3.12, FastAPI/uvicorn (existing), PyInstaller 6, WinSW 2.x, Inno Setup 6, pystray (Windows tray), rumps (macOS menu bar), nfpm, GitHub Actions, Cloudflare Pages.

**Spec:** `docs/specs/2026-11-19-easy-setup.md` (part 1, "One download per system", and the "Calls made in design" list).

## Global Constraints

- One port, 8710 by default, for everything.
- Existing Docker and Unraid installs upgrade with no change: `/data`, `/recordings`, `/library` and every `COUCHELEPHANT_*` variable keep their meaning.
- Data folders: Windows `%ProgramData%\CouchElephant`, macOS `~/Library/Application Support/CouchElephant`, Linux `/var/lib/couchelephant`.
- Supported: Windows 10 and 11 x64; macOS 13 and later, Apple silicon and Intel (two downloads, my call, below); Linux amd64 and arm64, `.deb` tested on Debian 12 and Ubuntu 22.04, `.rpm` on Fedora and RHEL 9 (Rocky 9 in CI).
- Windows ships unsigned for now (owner's decision). macOS is signed with Developer ID and notarized.
- ffmpeg and comskip ship as separate programs with their licences and a source offer in `THIRD_PARTY.md`.
- The download page lives at `https://couchelephant.app`; `https://couchelephant.app/install.sh` is the Linux one-liner.
- No host names, LAN addresses or people's names in code, tests or commits (public repo). No emoji or em dashes in UI text or docs. Say "TV", "Movies", "series".
- Every user-facing step has a test that drives it: pytest for the server, a CI smoke test per installer that installs, starts, and asks `GET /api/tv/hello`.

## Review Focus

- **The port is already taken** (an old Docker copy on the same machine): the installer must offer the next free port, and the service must start on it. Test in Task 4 and Task 5.
- **An install path or data path with spaces or non-ASCII letters** (`C:\Program Files`, a macOS user named with an accent): the service must start. Test in Task 1 (`data_dir` with a space) and the Windows smoke test (default `Program Files` path).
- **Upgrading over an older install keeps the data**: database, logos and paired devices survive a reinstall. Test in each installer's smoke job (install old, write a setting, install new, read it back).
- **A machine with no GPU and no `/dev/dri`**: recording still works with the bundled ffmpeg on the processor. Covered by the smoke test recording the fake tuner's stream in Task 3.
- **An update check with no internet**: the server starts and works, the check fails quietly and is retried tomorrow. Test in Task 8.

## Calls made in this plan

- macOS ships two downloads (Apple silicon, Intel) rather than one universal app: PyInstaller's universal2 needs a universal Python and universal copies of every native wheel, which two native builds avoid.
- The macOS one-click update replaces the app from the signed `.dmg` and checks its signature with `codesign --verify --deep --strict` and `spctl --assess`, instead of Sparkle (the spec named Sparkle; Sparkle expects a native Cocoa app, and this one is Python).
- Windows one-click update checks the installer's SHA-256 against the release's `SHA256SUMS` file, since the installer is unsigned for now.

---

### Task 1: One place for paths, port and programs

**Files:**
- Create: `app/paths.py`
- Modify: `app/db.py:16`, `app/auth.py:28`, `app/backups.py:114-115`, `app/dbstore.py:183`, `app/sync.py:9`, `app/portable.py:36`, `app/recorder.py:29-30,138`, `app/adskip.py:34`, `app/handover.py:26`
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces: `paths.data_dir() -> pathlib.Path`, `paths.port() -> int`, `paths.tool(name: str) -> str`, `paths.recordings_dir() -> pathlib.Path`, `paths.library_dir() -> pathlib.Path | None`, `paths.install_id() -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COUCHELEPHANT_DATA", str(tmp_path / "with space"))
    assert paths.data_dir() == tmp_path / "with space"
    assert paths.data_dir().is_dir()          # made on first use

def test_docker_keeps_data(monkeypatch):
    monkeypatch.delenv("COUCHELEPHANT_DATA", raising=False)
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert str(paths.data_dir()) == "/data"

@pytest.mark.parametrize("system,expect", [
    ("Windows", r"CouchElephant"), ("Darwin", "Application Support/CouchElephant"),
    ("Linux", "/var/lib/couchelephant")])
def test_native_defaults(monkeypatch, system, expect): ...   # platform.system patched

def test_port_default_and_env(monkeypatch):
    assert paths.port() == 8710
    monkeypatch.setenv("COUCHELEPHANT_PORT", "8711"); assert paths.port() == 8711

def test_tool_prefers_bundled(monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir(); exe = tmp_path / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_text(""); exe.chmod(0o755)
    monkeypatch.setattr(paths, "_bundle_root", lambda: tmp_path)
    assert paths.tool("ffmpeg") == str(exe)

def test_tool_env_then_path(monkeypatch):
    monkeypatch.setenv("COUCHELEPHANT_FFMPEG", "/opt/x/ffmpeg")
    assert paths.tool("ffmpeg") == "/opt/x/ffmpeg"

def test_install_id_is_stable(tmp_db):
    assert paths.install_id() == paths.install_id() and len(paths.install_id()) == 32
```

- [ ] **Step 2: Run them, expect failure**

Run: `.venv/bin/python -m pytest tests/test_paths.py -q`
Expected: FAIL, `ModuleNotFoundError: app.paths`.

- [ ] **Step 3: Implement `app/paths.py`**

Order for `data_dir`: `COUCHELEPHANT_DATA`, then `/data` when `_in_container()` (the file `/.dockerenv` exists or `COUCHELEPHANT_DB` starts with `/data/`), then the per-system default from Global Constraints; on Linux fall back to `~/.local/share/couchelephant` when `/var/lib/couchelephant` is not writable (a tarball run by hand). `tool(name)`: env var `COUCHELEPHANT_<NAME>` (existing names `COUCHELEPHANT_FFMPEG`, `COUCHELEPHANT_FFPROBE`, `COUCHELEPHANT_COMSKIP`), then `<bundle_root>/bin/<name>[.exe]` where `_bundle_root()` is `sys._MEIPASS` when frozen, else `None`, then the bare name. `recordings_dir()`: `COUCHELEPHANT_RECORDINGS` or `/recordings` in a container, else `data_dir()/recordings`. `library_dir()`: `COUCHELEPHANT_LIBRARY` or `/library` in a container, else `None` (part 2 fills absolute folders). `install_id()`: setting `install_id`, made once with `uuid.uuid4().hex`.

- [ ] **Step 4: Point every module at it**

Each listed `os.environ.get(..., "/data/...")` default becomes the `paths` call, with the environment variable still read first by `paths`. In `handover.py`, a `None` library dir means the configured folders are absolute paths (part 2 uses this).

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q --ignore=tests/ui && .venv/bin/python -m pytest -q tests/ui`
Expected: all pass (764 and 205 today, plus the new tests).

- [ ] **Step 6: Commit** `git commit -m "One place for the data folder, port and bundled programs"`

### Task 2: A command that runs the server

**Files:**
- Create: `app/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `paths.port()`, `paths.data_dir()`.
- Produces: `python -m app [--port N] [--open] [--no-open]`; `app.__main__.main(argv: list[str]) -> int`; `app.__main__.first_run() -> bool` (true until Plex is set).

- [ ] **Step 1: Failing tests**: `test_main_starts_and_answers_hello` (start `main(["--port", free_port, "--no-open"])` in a thread with `COUCHELEPHANT_DATA=tmp_path`, poll `GET /api/tv/hello` for 20 s, expect 200); `test_open_only_on_first_run` (patch `webbrowser.open`; called once with `http://localhost:<port>/welcome` when not configured, not at all when `plex_url` and `plex_token` are set).
- [ ] **Step 2: Run, expect failure.** `pytest tests/test_main.py -q` fails on import.
- [ ] **Step 3: Implement** with `uvicorn.Config("app.web:app", host="0.0.0.0", port=..., server_header=False)` and `uvicorn.Server.run()`; open the browser 2 s after start, in a thread, only with `--open` and `first_run()`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Change the Dockerfile `CMD` to `["python", "-m", "app", "--no-open"]`** and run `docker build` plus the existing container health check locally.
- [ ] **Step 6: Commit** `git commit -m "python -m app runs the server"`

### Task 3: A frozen server with its programs inside

**Files:**
- Create: `packaging/couchelephant.spec`, `packaging/tools.json`, `packaging/fetch_tools.py`, `packaging/smoke.py`, `packaging/build_comskip.sh`
- Modify: `THIRD_PARTY.md`, `requirements-dev.txt` (add `pyinstaller==6.*`)

**Interfaces:**
- Consumes: `app/__main__.py`.
- Produces: `dist/couchelephant/couchelephant[.exe]` (one folder) with `bin/ffmpeg`, `bin/ffprobe`, `bin/comskip`; `python packaging/fetch_tools.py <windows|macos|linux> <x64|arm64> <dest>`; `python packaging/smoke.py <path-to-executable>` exits 0 on success.

- [ ] **Step 1: Write `packaging/smoke.py`** that starts the executable with a temporary `COUCHELEPHANT_DATA` and a free port, waits for `GET /api/tv/hello` (60 s), checks `GET /static/css/app.css` is 200 (templates and static files are inside), runs `<bin>/ffmpeg -version` and `<bin>/comskip --help` (exit status ignored for comskip; it must start), then stops the process and checks the data folder holds `couchelephant.db`.
- [ ] **Step 2: Run it against a plain checkout to see it fail.** `python packaging/smoke.py dist/couchelephant/couchelephant` fails: no such file.
- [ ] **Step 3: Write `tools.json`** pinning, per system and architecture, the URL and SHA-256 of a static ffmpeg build (BtbN GPL builds for Windows x64 and Linux; a static macOS build for arm64 and x64), and `fetch_tools.py` that downloads, checks the hash, and unpacks only `ffmpeg`, `ffprobe` and the licence file into `<dest>/bin`. comskip: `build_comskip.sh` builds the commit the Dockerfile pins (`a140b6ac8bc8f596729e9052819affc779c3b377`) on Linux and macOS; Windows uses the upstream Windows build pinned by hash in `tools.json`.
- [ ] **Step 4: Write `couchelephant.spec`** (one folder, console off on Windows, `datas` for `app/templates`, `app/static`, `app/data`; `binaries` from the fetched `bin/`).
- [ ] **Step 5: Build and smoke-test on Linux.** `pyinstaller packaging/couchelephant.spec && python packaging/smoke.py dist/couchelephant/couchelephant`, expected `smoke ok`.
- [ ] **Step 6: Add the bundled programs' licences and source offer to `THIRD_PARTY.md`.**
- [ ] **Step 7: Commit** `git commit -m "Frozen server with ffmpeg and comskip inside"`

### Task 4: Linux packages and install.sh

**Files:**
- Create: `packaging/linux/nfpm.yaml`, `packaging/linux/couchelephant.service`, `packaging/linux/postinstall.sh`, `packaging/linux/preremove.sh`, `packaging/linux/install.sh`, `packaging/linux/test_install.sh`
- Test: CI job `linux-packages` (Task 7)

**Interfaces:**
- Consumes: the Task 3 folder.
- Produces: `couchelephant_<version>_<amd64|arm64>.deb`, `couchelephant-<version>.<x86_64|aarch64>.rpm`, `couchelephant-<version>-linux-<x64|arm64>.tar.gz`; the unit `couchelephant.service` running `/opt/couchelephant/couchelephant --no-open` as user `couchelephant`, `Environment=COUCHELEPHANT_DATA=/var/lib/couchelephant`, `Restart=on-failure`.

- [ ] **Step 1: Write `test_install.sh`**: in a container for each of `debian:12`, `ubuntu:22.04`, `fedora:40`, `rockylinux:9`, install the package with the system's tool, start the service (or run the unit's `ExecStart` directly where the container has no systemd), `curl -fs localhost:8710/api/tv/hello`; then occupy 8710 with `python3 -m http.server 8710`, reinstall, and expect the service on 8711 with `COUCHELEPHANT_PORT=8711` written to `/etc/default/couchelephant`; then upgrade over a version that wrote a setting and read it back.
- [ ] **Step 2: Run it, expect failure** (no package yet).
- [ ] **Step 3: Write the nfpm config, unit, and scripts.** `postinstall.sh` makes the system user, the data folder, picks the port (first free from 8710 up, kept in `/etc/default/couchelephant`), enables and starts the unit, and prints the address to open.
- [ ] **Step 4: Write `install.sh`**: detect architecture and `ID`/`ID_LIKE` from `/etc/os-release`, read the latest release from `https://api.github.com/repos/datbird/couchelephant/releases/latest`, install the `.deb` or `.rpm`, or the tarball plus the unit for anything else; refuse without root and say to rerun with sudo; `shellcheck` clean.
- [ ] **Step 5: Run `test_install.sh`, expect all four distros to pass.**
- [ ] **Step 6: Commit** `git commit -m "Linux packages, a service and install.sh"`

### Task 5: Windows installer, service and tray

**Files:**
- Create: `packaging/windows/couchelephant.iss`, `packaging/windows/couchelephant-service.xml` (WinSW), `app/tray.py`, `packaging/windows/tray.spec`, `packaging/windows/test_install.ps1`
- Test: `tests/test_tray.py` (menu wiring, no display needed)

**Interfaces:**
- Consumes: Task 3 folder, `paths.port()`.
- Produces: `CouchElephant-Setup-<version>.exe`; service name `CouchElephant`; tray program `CouchElephantTray.exe` started at login, menu Open, Pause recordings, Quit; `app.tray.build_menu(port: int) -> list[tuple[str, Callable]]`.

- [ ] **Step 1: Failing tests**: `test_tray_menu` asserts the labels `["Open", "Pause recordings", "Quit"]` and that Open calls `webbrowser.open("http://localhost:<port>/")`; Pause posts to the existing recorder pause setting (find the route that sets `recorder_default`, or add `POST /settings/recorder/pause` if none exists, with a test). `test_install.ps1`: silent install (`/VERYSILENT`), service running, `Invoke-WebRequest http://localhost:8710/api/tv/hello` 200, firewall rule exists with profile `Private` only, data in `C:\ProgramData\CouchElephant`, port-taken case uses 8711, uninstall keeps data unless `/DELETEDATA=1`.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** the Inno Setup script (Next, Next, Finish; port check in `[Code]` with `netstat` or a socket bind; `netsh advfirewall firewall add rule name="CouchElephant" dir=in action=allow protocol=TCP localport=<port> profile=private`; WinSW installs the service; opens `http://localhost:<port>/welcome` at Finish), the WinSW XML, and `app/tray.py` with pystray.
- [ ] **Step 4: Run the tests and the PowerShell smoke test on a Windows runner, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "Windows installer, service and tray"`

### Task 6: macOS menu bar app

**Files:**
- Create: `app/menubar.py`, `packaging/macos/menubar.spec`, `packaging/macos/entitlements.plist`, `packaging/macos/make_dmg.sh`, `packaging/macos/sign_and_notarize.sh`, `packaging/macos/test_app.sh`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: Task 3, `paths`.
- Produces: `CouchElephant-<version>-macos-<arm64|x64>.dmg` with `CouchElephant.app` (`LSUIElement` true); menu Open, Start at login (on/off), Quit; `app.menubar.login_item_path() -> Path` (`~/Library/LaunchAgents/app.couchelephant.plist`).

- [ ] **Step 1: Failing tests**: menu labels; Start at login writes and removes the LaunchAgent plist with `ProgramArguments` pointing at the app's executable and `RunAtLoad` true; the server runs in a thread in the same process and stops on Quit.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** with rumps; freeze with the bundle spec; `make_dmg.sh` uses `hdiutil create -srcfolder` with an Applications link.
- [ ] **Step 4: Signing prerequisite.** Create a Developer ID Application certificate for team R2WL3RVV3S (App Store Connect API key in 1Password "BalanceEngine TestFlight Query"; if the API refuses this certificate type, the Account Holder creates it at developer.apple.com and exports a .p12). Store the .p12 and its password as GitHub secrets `MACOS_DEVID_P12`, `MACOS_DEVID_P12_PASSWORD`, plus `ASC_KEY_ID`, `ASC_ISSUER`, `ASC_KEY_P8`.
- [ ] **Step 5: `sign_and_notarize.sh`** signs every binary inside out with hardened runtime and the entitlements, then `xcrun notarytool submit --wait` with the API key, then `xcrun stapler staple`.
- [ ] **Step 6: `test_app.sh`**: mount the dmg, copy the app to a temp folder, `spctl --assess --type execute` passes, start it, `curl localhost:8710/api/tv/hello` 200, quit.
- [ ] **Step 7: Commit** `git commit -m "macOS menu bar app, signed and notarized"`

### Task 7: Build everything on a tag

**Files:**
- Modify: `.github/workflows/release.yml`
- Create: `.github/workflows/installers.yml` (reusable, also runs on pull requests touching `packaging/` or `app/paths.py`)

**Interfaces:**
- Produces: release assets named as in Tasks 4 to 6, plus `SHA256SUMS`.

- [ ] **Step 1: Matrix** `ubuntu-latest` (x64) and `ubuntu-24.04-arm` (arm64) for Task 3 plus Task 4; `windows-latest` for Task 5; `macos-14` (arm64) and `macos-13` (x64) for Task 6. Each job runs its smoke test before uploading its files.
- [ ] **Step 2: A final job** gathers the files, writes `SHA256SUMS`, and attaches all of it to the GitHub release the tag made, after the existing `test` job and next to the Docker publish.
- [ ] **Step 3: Run it** with `workflow_dispatch` on a branch; expect every smoke step green and the files listed on a draft release.
- [ ] **Step 4: Commit** `git commit -m "Build and test the installers on a tag"`

### Task 8: Update available, and one click to update

**Files:**
- Create: `app/updates.py`
- Modify: `app/web.py` (a daily loop like `backup_loop`), `app/templates/base.html` (a banner for administrators), `app/tray.py`, `app/menubar.py`
- Test: `tests/test_updates.py`, a Playwright test in `tests/ui/test_updates.py`

**Interfaces:**
- Consumes: `VERSION` from `app/routes/_shared.py`.
- Produces: `updates.latest() -> Release | None` (`Release(version: str, url: str, assets: dict[str, str])`), `updates.available() -> Release | None`, `GET /api/update` -> `{"ok": true, "current": str, "latest": str | null, "how": "click" | "command" | "docker"}`, `POST /api/update/apply` (administrators; Windows and macOS only).

- [ ] **Step 1: Failing tests**: a fake releases feed newer than `VERSION` makes `available()` return it; an older or equal one returns None; a network error returns None and logs one line, and the server keeps running; `how` is `docker` in a container, `command` on Linux, `click` on Windows and macOS; on Windows, `apply` refuses an installer whose SHA-256 does not match `SHA256SUMS`; the banner shows "Update available: <version>" to an administrator and not to others.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement.** Check `https://api.github.com/repos/datbird/couchelephant/releases/latest` once a day. Windows apply: download to `%TEMP%`, check the hash, run it with `/SILENT`, which restarts the service. macOS apply: download the dmg, `codesign --verify --deep --strict` and `spctl --assess` the app inside, replace `/Applications/CouchElephant.app`, relaunch. Linux: show `curl -fsSL https://couchelephant.app/install.sh | sudo sh`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "Update available, and one click to update"`

### Task 9: couchelephant.app

**Files:**
- Create: `site/index.html`, `site/install.sh` (copied from `packaging/linux/install.sh` by the workflow), `site/_headers`, `.github/workflows/site.yml`
- Modify: `docs/INSTALL.md`, `README.md` ("Getting started" leads with the download page; Docker stays as the second way)

**Interfaces:**
- Produces: `https://couchelephant.app/` (one big button for the visitor's system from the user agent, the other systems under it, Docker and Unraid last) and `https://couchelephant.app/install.sh`.

- [ ] **Step 1: Failing test**: `tests/ui/test_site.py` opens `site/index.html` with a Windows, a macOS (Apple silicon and Intel), and a Linux user agent, and asserts the first button names that system and links to the matching asset of the latest release (a fixture feed); a page with no script still lists every download.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Write the page** in the app's existing look (dark, the elephant logo from `app/static`), plain HTML and a little script; `_headers` sets `Content-Type: text/x-shellscript` for `install.sh`.
- [ ] **Step 4: Cloudflare Pages.** Create the Pages project and attach `couchelephant.app` (zone already on the datbird account) with `cf raw`; the workflow deploys `site/` with `wrangler pages deploy` using a Pages-scoped token stored as `CLOUDFLARE_PAGES_TOKEN`.
- [ ] **Step 5: Run the test, deploy, then check** `curl -fsSI https://couchelephant.app/` is 200 and `curl -fsS https://couchelephant.app/install.sh | head -1` is `#!/bin/sh`.
- [ ] **Step 6: Commit** `git commit -m "couchelephant.app download page"`
