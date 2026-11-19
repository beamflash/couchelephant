# Easy setup, part 3: the apps find the server - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Apple apps list the CouchElephant servers on the home network by name, and one tap connects; a QR code on the setup page does the same for a phone.

**Architecture:** The server announces `_couchelephant._tcp` with Bonjour through the `zeroconf` package, started and stopped with the app. The apps browse with Network.framework's `NWBrowser`, list each install once by its id, and use the existing connect and pairing flow. A QR code (`segno`) on the welcome page and in Settings, Televisions opens `couchelephant://connect?url=...`.

**Tech Stack:** Python `zeroconf` (LGPL-2.1, used unmodified as a library), `segno` (BSD), SwiftUI, Network.framework, XCUITest.

**Spec:** `docs/specs/2026-11-19-easy-setup.md` (part 3, and "The apps list each server once").

## Global Constraints

- Service type `_couchelephant._tcp`, on the server's port. TXT keys: `id` (install id), `v` (version), `name` (computer name), `tls` (`0` or `1`), `fp` (SHA-256 of the public key, base64url, only when `tls=1`).
- On by default; setting `announce` (`1`/`0`) under Settings turns it off.
- No key, token or password in TXT records or QR codes.
- The typed address stays, below the list.
- Info.plist keeps `NSLocalNetworkUsageDescription` and gains `NSBonjourServices` = `_couchelephant._tcp`; the usage text says why in plain words (App Review reads it).
- Docker on its default bridge network cannot announce; the Docker docs and Unraid template say to use host networking for discovery or to type the address.

## Review Focus

- **Two network interfaces** (Wi-Fi and Ethernet, or a VPN): the server is listed once, and the address used is one the device can reach. Test in Task 3 (dedupe by `id`, prefer an address on the device's own subnet).
- **The server's address changes** (DHCP): a paired app finds it again by id and updates the stored address without pairing again. Test in Task 3.
- **Two installs on one network** (Docker copy and a native copy): both listed, each by its own name and port. Test in Task 1 and Task 3.
- **Local network permission denied on the device**: the list says "Allow CouchElephant to find devices on your network in Settings" and the typed address still works. Test in Task 3 with the browser's `.failed` state injected.
- **A guest Wi-Fi that blocks Bonjour**: after 5 seconds with nothing found, the screen leads with the typed address and the last address that worked. Test in Task 3.

---

### Task 1: The server announces itself

**Files:**
- Create: `app/announce.py`
- Modify: `app/web.py` (startup and shutdown), `app/routes/settings.py` (the `announce` setting in `apply`), `app/templates/_settings.html`, `requirements.txt` (`zeroconf==0.*` pinned to the current release)
- Test: `tests/test_announce.py`

**Interfaces:**
- Consumes: `paths.install_id()`, `paths.port()`, `VERSION`.
- Produces: `announce.start(port: int) -> None`, `announce.stop() -> None`, `announce.txt() -> dict[str, str]`, `announce.refresh() -> None` (called when `tls` or `fp` change in part 4).

- [ ] **Step 1: Failing tests**: `txt()` has `id`, `v`, `name`, `tls="0"` and no `fp`; with `zeroconf` started on the loopback interface, a `ServiceBrowser` in the test finds `_couchelephant._tcp.local.` with that TXT and port within 5 s; setting `announce` to `0` and calling `refresh()` withdraws it; `start` failing (no multicast) logs one line and the server keeps running.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** with `zeroconf.asyncio.AsyncZeroconf`, instance name `CouchElephant on <hostname>`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "Announce the server on the home network"`

### Task 2: QR code to connect

**Files:**
- Modify: `app/routes/tv.py` (beside `hello`), `app/templates/welcome.html`, `app/templates/_settings.html` (Televisions), `requirements.txt` (`segno`)
- Test: `tests/test_connect_qr.py`, `tests/ui/test_connect_qr.py`

**Interfaces:**
- Produces: `GET /api/connect.svg` (an SVG QR of `couchelephant://connect?url=<lan url>` and, with TLS on, `&fp=<fp>`); `tv.lan_url(request) -> str` (the request's own host when it is a LAN address, else the first non-loopback IPv4 of the machine, with the port).

- [ ] **Step 1: Failing tests**: decoding the SVG's payload (generate with `segno` and compare `segno.make(...).svg_inline()` for the expected text) gives the expected URL; the payload has no `key` or `token`; with sign-in on, the route is administrator-only.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement**, and show the QR with one line "Scan with your iPhone or iPad camera" on the welcome page (after Plex is set) and in Settings, Televisions.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "QR code to connect a phone"`

### Task 3: The apps list servers they find

**Files:**
- Create: `clients/apple/Shared/Discovery.swift`
- Modify: `clients/apple/Phone/SetupView.swift`, `clients/apple/TV/SetupView.swift`, `clients/apple/Shared/Hub.swift` (`openLink` handles `couchelephant://connect`), `clients/apple/Shared/AppModel.swift` (re-find a paired server by id), `clients/apple/Info.plist`, `clients/apple/UITests/Phone/PhoneTests.swift`, `clients/apple/UITests/TV/TVTests.swift`

**Interfaces:**
- Produces: `struct FoundServer: Identifiable, Hashable { let id: String; let name: String; let url: String; let fingerprint: String? }`; `@MainActor final class Discovery: ObservableObject { @Published var servers: [FoundServer]; @Published var state: State /* .looking, .found, .nothing, .notAllowed */; func start(); func stop() }`. DEBUG launch argument `-found "Name|http://host:port|id"` fills `servers` without the network, for tests (the simulator in the build VM and the test server are on different networks, so Bonjour cannot reach it).

- [ ] **Step 1: Failing XCUITests** (Phone and TV): launched with no stored address and `-found "CouchElephant on TEST|<test server>|abc"`, the first screen lists "CouchElephant on TEST"; a tap (TV: select) connects to the test server and reaches Home (sign-in is off on the test server); launched with `-found` empty and a 5 s wait, "Enter the address yourself" leads and the "no server" line links to `https://couchelephant.app`; `couchelephant://connect?url=<test server>` opened with the existing `-openlink` argument connects the same way.
- [ ] **Step 2: Run them in the VM, expect failure.**
- [ ] **Step 3: Implement `Discovery`** with `NWBrowser(for: .bonjourWithTXTRecord(type: "_couchelephant._tcp", domain: nil), using: .tcp)`; resolve each result to a URL with an `NWConnection` to the endpoint and read its remote IPv4 and port; dedupe by TXT `id`, preferring an address on the device's own subnet; map `.failed` with a policy-denied error to `.notAllowed`.
- [ ] **Step 4: SetupView** shows the list above the typed field, the 5 s fallback, and the `.notAllowed` line. `AppModel`: when the stored address fails and Discovery finds the stored install id at a new URL, switch to it and keep the device key.
- [ ] **Step 5: Run the Phone and TV suites in the VM** (the way `couchelephant-apple-ui-tests` memory describes), expect pass on iPhone 17, iPad Pro 13-inch, Apple TV 4K.
- [ ] **Step 6: Commit** `git commit -m "Apps list the servers they find"`

### Task 4: Docs, template and changelog

**Files:**
- Modify: `docs/INSTALL.md` (discovery, host networking for Docker), `CHANGELOG.md`, `docs/API.md` (`/api/connect.svg`), the Unraid template in the `datbird/unraid-templates` repo (network type note; ask before pushing there, it is a shared repo)

- [ ] **Step 1: Write them**, bump the patch version.
- [ ] **Step 2: Run** `pytest -q tests/test_conventions.py`, expect pass.
- [ ] **Step 3: Commit** `git commit -m "Docs: finding the server"`
