# Easy setup, part 4: safe away from home - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One switch, Use away from home, makes the single port safe to expose to the internet; off by default and never required.

**Architecture:** A home-network check (`app/exposure.py`) decides who is outside. With the switch on, a gate in front of the existing auth gate lets outsiders reach only sign-in, pairing and passed video links; sign-in attempts are limited; security headers go on every answer. The port serves HTTPS (own certificate, or a real one for a domain through ACME TLS-ALPN on the same port) and still answers plain HTTP at home, by peeking at each new connection's first byte. The apps pin the server's public key, and play video through a small loopback proxy so AVPlayer never sees the self-signed certificate.

**Tech Stack:** Python 3.12, uvicorn internals (`H11Protocol`/`HttpToolsProtocol`), `cryptography` (already installed through `pyjwt[crypto]`), `acme` and `josepy` (Let's Encrypt), SwiftUI, Network.framework, URLSession, XCUITest, Playwright.

**Spec:** `docs/specs/2026-11-19-easy-setup.md` (part 4, and the calls "Away from home needs a password", "The apps pin the public key", "First pairing happens at home").

## Global Constraints

- Off by default. An upgraded install is unchanged until the owner turns it on.
- The switch refuses to turn on while any way in from outside has no password: sign-in Off, or Local accounts with an account that has none. It offers to switch to Local accounts and make the first account there. With Cloudflare Access, Cloudflare does the sign-in.
- From outside, only these answer without a signed-in session: `/login`, `/static/*`, `/favicon.ico`, `/api/tv/hello`, `/api/tv/pair`, `/api/tv/pair/poll` (only to a server with a real certificate, see below), requests carrying a paired device key on the paths `_device_may` allows, and `/hlsk/*` with a valid pass. Never `/setup`, `/welcome`, `/settings*`, `/docs`, `/openapi.json`, `/redoc`.
- Home network: loopback, RFC 1918, link-local, IPv6 ULA and the server's own subnets. `X-Forwarded-For` counts only from addresses in the setting `trusted_proxies`.
- The own certificate: ECDSA P-256, ten years, in `<data_dir>/tls/`, made once; a reinstall without the old data makes a new key.
- Sign-in limits: per address and per account, 5 failures in 10 minutes locks for 1 minute, doubling per further lockout to a 1 hour cap; each failure logged with the address, never the password.
- Headers on every answer: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, a `Content-Security-Policy` the existing pages pass (check with the browser suite), `Strict-Transport-Security: max-age=31536000` on HTTPS answers only; no `Server` header.
- No emoji or em dashes; plain words in every message.

## Review Focus

- **A home device that reaches the server through the router's public address** (hairpin NAT): it is treated as outside and must sign in. Say so in the self-check, and test that a request from the public address with no session gets the sign-in page, not the app.
- **The owner turns the switch on while signed in from outside** (through a VPN that looks outside): the change must not lock them out mid-session. Test in Task 2 that the session that turned it on stays valid.
- **A paired TV keeps working through the change**: devices paired before the switch keep their keys; the apps learn the pin the next time they are home (the `fp` TXT record), and until then use the address they had at home over HTTP. Test in Task 5.
- **Live video through the app's proxy while the phone sleeps and wakes**: the proxy restarts on its next request and the player resumes; it never serves to anything but loopback. Test in Task 5.
- **A second certificate request while one is running** (two quick saves of the domain): only one ACME order runs. Test in Task 4.

## Calls made in this plan

- **One port for HTTP and HTTPS by peeking.** A TLS handshake starts with byte `0x16`; anything else is plain HTTP. The listener peeks with `MSG_PEEK` and hands the socket to uvicorn's own protocol, with or without TLS, so the client address stays true. Plain HTTP from outside gets `308` to the same URL on HTTPS.
- **The apps play video through a loopback proxy when the server's certificate is their pin.** AVPlayer cannot be told to trust a pinned certificate, so the app serves `http://127.0.0.1:<port>/...` itself and fetches from the server with a pinned URLSession. With a real certificate the proxy is not used. AirPlay from a phone away from home with a self-signed server sends the TV a loopback address it cannot open; the app says to use a real domain for that.
- **First pairing from outside** is allowed only when the server presents a certificate the system trusts (a real domain).

---

### Task 1: Who is at home

**Files:**
- Create: `app/exposure.py`
- Test: `tests/test_exposure.py`

**Interfaces:**
- Produces: `exposure.client_address(request) -> ipaddress.IPv4Address | IPv6Address`; `exposure.is_home(request) -> bool`; `exposure.away_on() -> bool` (setting `away_mode` == `"1"`); `exposure.local_subnets() -> list[ipaddress.IPv4Network | IPv6Network]`.

- [ ] **Step 1: Failing tests** (parametrised): `127.0.0.1`, `10.1.2.3`, `172.16.0.9`, `192.168.1.5`, `169.254.1.1`, `::1`, `fd00::1` are home; `8.8.8.8`, `2001:4860::8888`, `100.64.0.1` (CGNAT) are not; `X-Forwarded-For: 8.8.8.8` from `192.168.1.1` is home unless `trusted_proxies` contains `192.168.1.1`, then outside; a malformed `X-Forwarded-For` from a trusted proxy is outside; IPv4-mapped IPv6 `::ffff:192.168.1.5` is home.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** with `ipaddress`. `local_subnets()` reads the machine's own addresses with `socket.getaddrinfo(socket.gethostname(), None)` and adds a /24 (IPv4) or /64 (IPv6) around each; the private ranges above already cover a normal home, so no new dependency.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "Tell home from outside"`

### Task 2: The switch, the gate, limits and headers

**Files:**
- Create: `app/ratelimit.py`
- Modify: `app/web.py` (a `_away_gate` middleware registered so it runs before `_auth_gate`; a headers middleware; `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` when away is on, checked per request), `app/routes/account.py` (login counts failures), `app/routes/tv.py` (`pair_start` counts), `app/routes/settings.py` (`POST /settings/away`), `app/templates/_settings.html`
- Test: `tests/test_away.py`, `tests/ui/test_away.py`

**Interfaces:**
- Consumes: Task 1, `auth.mode()`, `auth.list_users()`.
- Produces: `POST /settings/away` form `on=1|0` -> `{"ok": true}` or `{"ok": false, "error": str, "fix": "make_account" | "set_passwords"}`; `ratelimit.check(key: str) -> float` (seconds still locked, 0 when free), `ratelimit.fail(key: str) -> None`, `ratelimit.ok(key: str) -> None`; `GET /api/away/check` -> `{"items": [{"name": str, "ok": bool, "say": str}]}` for the self-check.

- [ ] **Step 1: Failing API tests** (TestClient with a `client` address patched per request): with away on and the client outside, `/` redirects to `/login`, `/settings`, `/welcome`, `/setup`, `/docs`, `/openapi.json` are 404 or 401, `/api/tv/hello` answers, a device key reaches `/api/tv/home`, `/hlsk/` with a good pass streams and with a bad one is 401; the same requests from home behave exactly as today; turning on with sign-in Off returns `fix: make_account`; with a local account and Cloudflare mode it turns on; the session that turned it on keeps working; the sixth wrong password in 10 minutes from one address is refused for 60 s with "Too many tries. Wait a minute.", the next lockout 120 s; headers present, `Server` absent, HSTS only on HTTPS.
- [ ] **Step 2: Failing Playwright test**: Settings shows the switch off; turning it on with sign-in Off shows "Make an account first" with the form; after making one the switch turns on and the self-check lists what is safe and what is not, including "Your router may send home devices through the internet address; they will be asked to sign in."
- [ ] **Step 3: Run, expect failure.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run the server and browser suites, expect pass** (the CSP must not break any page; the browser suite is the check).
- [ ] **Step 6: Commit** `git commit -m "Use away from home: gate, limits, headers"`

### Task 3: HTTPS on the same port

**Files:**
- Create: `app/tls.py`, `app/oneport.py`
- Modify: `app/__main__.py` (part 1, Task 2) to serve through `oneport.serve(config)`; `app/announce.py` (`tls`, `fp` in TXT, then `refresh()`)
- Test: `tests/test_tls.py`, `tests/test_oneport.py`

**Interfaces:**
- Produces: `tls.ensure_own() -> OwnCert` (`OwnCert(cert_path: Path, key_path: Path, fingerprint: str)`, fingerprint = base64url SHA-256 of the SubjectPublicKeyInfo DER), `tls.context() -> ssl.SSLContext | None` (None while away is off); `oneport.serve(config: uvicorn.Config) -> None`.

- [ ] **Step 1: Failing tests**: `ensure_own()` makes the files once and returns the same fingerprint on the second call; the certificate is valid for ten years and names the host and the LAN addresses; with away on, an `httpx` client over TLS (verify off, fingerprint checked by hand) and a plain HTTP client both reach `/api/tv/hello` on the same port; plain HTTP from an outside address (patched) gets 308 to `https://`; the request's client address in the app is the real one, not loopback; with away off the port is plain HTTP only, as today.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** `oneport.serve`: create the listening socket, and for each accepted socket peek one byte with `MSG_PEEK`, then `loop.connect_accepted_socket(lambda: Protocol(config=config, server_state=state, app_state=app_state), sock, ssl=ctx if first == 0x16 else None)` with uvicorn's configured HTTP protocol class; run uvicorn's lifespan as `uvicorn.Server` does. If a uvicorn upgrade changes these internals, the test above fails first.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "HTTPS and HTTP on one port"`

### Task 4: A real certificate for a domain

**Files:**
- Create: `app/acme_tls.py`
- Modify: `app/routes/settings.py` (`POST /settings/away/domain`), `app/tls.py` (`context()` prefers the real certificate and answers the ALPN `acme-tls/1` challenge through an `sni_callback`), `requirements.txt` (`acme`, `josepy`)
- Test: `tests/test_acme_tls.py` (against Pebble, Let's Encrypt's test server, in a CI service container)

**Interfaces:**
- Produces: `acme_tls.request(domain: str) -> None` (one order at a time, a lock), `acme_tls.renew_if_due() -> None` (daily, 30 days before expiry), setting `away_domain`.

- [ ] **Step 1: Failing tests** against Pebble: an order for `test.local` completes through TLS-ALPN on the one port and `tls.context()` then serves that certificate for that name and the own one for any other name; two quick calls run one order; a failure keeps the own certificate and the self-check says why ("Let's Encrypt could not reach this server on port 443; forward port 443 on your router to port <port> on this computer").
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement.** Production directory `https://acme-v02.api.letsencrypt.org/directory`, account key in `<data_dir>/tls/acme-account.pem`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "A real certificate for a domain, on the same port"`

### Task 5: The apps pin the key and play through a loopback proxy

**Files:**
- Create: `clients/apple/Shared/Pinning.swift`, `clients/apple/Shared/LoopbackProxy.swift`
- Modify: `clients/apple/Shared/Server.swift` (the session uses the pinning delegate; the pin is stored in the keychain beside the device key, per server id), `clients/apple/Shared/PlayerView.swift` (stream URLs go through the proxy when the pin, not the system, vouched for the server), `clients/apple/Shared/Discovery.swift` (read `fp`), `clients/apple/Shared/AppModel.swift` (the "Away from home is off on your server" message), UITests
- Test: XCUITests, plus `tests/test_tls.py` providing the server side

**Interfaces:**
- Consumes: TXT `fp` (part 3), QR `fp` (part 3, Task 2), `tls.ensure_own().fingerprint` format.
- Produces: `final class PinningDelegate: NSObject, URLSessionDelegate { init(pin: String?) }` accepting a system-trusted chain, or a leaf whose SPKI SHA-256 (base64url) equals the pin; `final class LoopbackProxy { static let shared; func start() throws -> Int /* port */; func url(for remote: URL) -> URL }` bound to `127.0.0.1` only, forwarding GET with the device key header and streaming the body.

- [ ] **Step 1: Failing XCUITests** with the test server's away mode on (a DEBUG test-server switch sets `away_mode=1`, a local account, and makes the own certificate): the app paired at home over HTTP, then launched with the server's address as `https://<test server>:<port>` (it counts as away for the app only by the URL), loads Home, plays a recording and live TV through the proxy (the probe reports progress), and a wrong pin (launch argument `-pin AAAA`) shows "This is not the server you paired with. Pair again at home."
- [ ] **Step 2: Run in the VM, expect failure.**
- [ ] **Step 3: Implement.** The proxy uses `NWListener` on `127.0.0.1`, port 0, and restarts on the next request after the app wakes.
- [ ] **Step 4: Run the Phone and TV suites in the VM, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "Apps pin the server's key away from home"`

### Task 6: Docs and changelog

**Files:**
- Modify: `docs/AUTH.md` (a section "Away from home"), `docs/INSTALL.md`, `docs/API.md`, `CHANGELOG.md`, `app/routes/_shared.py` (patch version)

- [ ] **Step 1: Write them**: what the switch does, what to forward on the router (the port, and 443 for a real certificate), and that a VPN or an existing tunnel works with the switch off.
- [ ] **Step 2: Run** `pytest -q tests/test_conventions.py`, expect pass.
- [ ] **Step 3: Commit** `git commit -m "Docs: away from home"`
