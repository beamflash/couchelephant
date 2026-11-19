# Easy setup: one download, Sign in with Plex, found on its own, safe away from home

Status: design, 2026-11-19. Plan to follow in `docs/plans/`.

## Why

The Apple apps are about to be sold (one-time, $9.99, universal purchase). A
buyer has Plex with a tuner and no CouchElephant server. Today they need
Docker, a Plex token copied out of `Preferences.xml`, a typed server address
in the app, and volume mounts to turn the recorder on. That is fine for the
people who run Unraid and wrong for someone who just paid for an app.

Success is the first ten minutes going like this:

1. The app opens on "No CouchElephant server found" and a link to the download
   page.
2. The download page offers the one file for the visitor's computer.
3. The installed server starts by itself and opens its setup page in the
   browser. One button: **Sign in with Plex**.
4. The app now lists the server by name ("CouchElephant on OFFICE-PC"). One
   tap, approve the device, done. A phone can also scan a QR code on the setup
   page.
5. Live TV plays.

## Goals

- **One download per system, nothing to install first.** Windows installer,
  Mac app, Linux package or one command. Python, ffmpeg and comskip ship
  inside. Docker and the Unraid template stay for people who want them.
- **Sign in with Plex** replaces the token and the address: the plex.tv/link
  code, then the Plex server is chosen from the account.
- **The apps find the server on the home network** with no typing. Typing an
  address stays possible.
- **Safe to expose the one port to the internet, when the owner chooses.** Off
  by default. Exposing it is never required.
- **Fewer settings to start recording.** The recorder's folders and the Plex
  library folders are filled in from what Plex reports, where that can be
  known.

## Non-goals

- A relay or cloud service run by us. Away from home means the owner opens a
  port, uses a VPN, or uses a tunnel they already have. We would otherwise
  run and pay for a service forever.
- Accounts on a CouchElephant website. The only account is the owner's Plex
  account.
- Replacing Plex DVR, or supporting tuners Plex does not see.
- Android, Windows or web store apps.

## Anti-goals (a failure even if it works)

- An installer that needs a terminal on Windows or macOS.
- A security warning the owner has to click through on a normal install
  (SmartScreen "unknown publisher", macOS "cannot be opened"). Signing is
  part of done.
- Exposing the port and having anything readable without signing in.
- A Plex token or device key written to a log, a URL or a QR code.
- Breaking the existing Docker and Unraid installs. They upgrade in place and
  keep their settings.

## The parts

Four projects, done in this order. Each ships on its own.

### 1. One download per system

What the owner sees:

- **Windows**: `CouchElephant-Setup-x.y.z.exe`. Next, Next, Finish. It installs
  a Windows service that starts with the computer, a tray icon (Open,
  Pause recordings, Quit), a firewall rule for the port on private networks
  only, and opens the setup page. Data in `%ProgramData%\CouchElephant`.
  Uninstall leaves the data unless asked.
- **macOS**: `CouchElephant-x.y.z.dmg`, drag to Applications. A menu bar app
  that starts the server and can start at login. Signed with Developer ID and
  notarized. Data in `~/Library/Application Support/CouchElephant`.
  Apple silicon and Intel (universal, or two downloads).
- **Linux**: `.deb` and `.rpm` for amd64 and arm64, and a tarball with
  `install.sh` for anything else. All three install a systemd service running
  as its own user, data in `/var/lib/couchelephant`. One line on the download
  page: `curl -fsSL https://<site>/install.sh | sh`, which picks the right one.
- **Docker / Unraid**: unchanged, same image, same template.

How it is made (builder's choice within these lines):

- The server is frozen with its own Python (python-build-standalone or
  PyInstaller) per system and architecture. No system Python is used.
- ffmpeg and comskip are bundled as separate programs, with their licences
  and source offer in `THIRD_PARTY.md`, the same terms the Docker image
  already meets. Hardware decoding stays optional: it is used where the
  system offers it and skipped where it does not.
- The app finds its programs, data folder and port from one place per system
  (today these are environment variables set by Docker; the defaults above
  apply when they are not set).
- GitHub Actions builds all of it on a version tag, next to the Docker image,
  and attaches it to the GitHub release.
- Update: each install checks the GitHub releases feed once a day and says
  "Update available" in the web app and the tray/menu bar. One click on
  Windows and macOS; on Linux the package manager or the same `install.sh`.

### 2. Sign in with Plex

- Settings, Plex gets one button, **Sign in with Plex**. It shows a four
  character code and opens `plex.tv/link`. The server asks plex.tv for the PIN
  result until the owner approves.
- With the token, the server lists the owner's Plex servers from plex.tv and
  picks the one with a DVR. More than one: the owner picks from a list by
  name. The connection tried first is the local address Plex reports, then
  the others.
- The token is stored as it is today and never shown again.
- The typed address and token stay under "Enter it yourself" for setups
  plex.tv cannot see.
- The first-run page (shown until Plex is set) is just this button and a
  line about what happens next.
- Once Plex is set, the recorder settings are filled in where Plex tells us:
  the tuner (already read from Plex), and the TV and Movies library folders
  when CouchElephant runs on the same machine as Plex and can write to them.
  When it cannot, the recorder keeps recordings in its own folder and says
  so, as it does now.

### 3. The apps find the server

- The server announces itself on the home network with Bonjour (mDNS), as
  `_couchelephant._tcp`, with its name, version and an install id in the TXT
  record. On by default; a switch in Settings turns it off.
- The Apple apps browse for it on their first screen and list what they find
  by name. One tap fills the address and starts the device approval that
  already exists (the six letter code). With sign-in off, a tap is all.
- "Enter the address yourself" stays, below the list.
- The setup page shows a QR code that opens the app with the address
  (`couchelephant://connect?url=...`). It carries no key or token. The
  approval still happens after it.
- The app's "no server found" screen links to the download page.
- Docker on its default bridge network cannot announce on the home network.
  The Docker instructions and Unraid template say to use host networking for
  discovery, or to type the address. Nothing else changes for them.

### 4. Safe away from home (optional)

A switch under Settings: **Use away from home**. Off by default. Turning it
on:

- **Requires sign-in for every request that does not come from the home
  network.** Sign-in mode Off still works at home; from outside, nothing but
  the sign-in page and the app pairing endpoints answer. The home network is
  the private address ranges and the server's own subnet; requests that came
  through a proxy are judged by the proxy's forwarded address only when that
  proxy is one the owner named.
- **Turns on HTTPS on the same port.** The server makes its own certificate.
  The apps remember that certificate when they pair at home and trust only it
  afterwards, so they are safe on any address with no domain. Browsers will
  warn on that certificate, so the owner can also give a domain name and the
  server gets a real certificate for it (Let's Encrypt, TLS-ALPN on the same
  port, with 443 forwarded to it), or use a reverse proxy or tunnel they
  already have.
- **Limits sign-in attempts** per address and per account, with a lockout that
  grows, and logs each failure.
- **Sends the usual security headers** (HSTS once HTTPS is on, no framing,
  content type, a content security policy the pages already meet).
- **Hides what an outsider does not need**: no version in headers, no API
  documentation pages, no setup or Plex sign-in endpoints from outside.
- **A self-check** on the same page lists what is safe and what is not: HTTPS
  on, a password on every account, Plex token stored, update available. It
  does not try to reach the server from outside; it says how to test that.
- The video stream links already carry a signed, short-lived pass for players
  that cannot send a header; that stays, and those passes are checked to work
  over HTTPS.

The apps: an address that is not on the home network is used over HTTPS with
the pinned certificate. A failure says "Away from home is off on your server"
rather than a network error.

## Constraints

- One port (8710) for everything, as now.
- Existing installs upgrade without a change: Docker volumes, environment
  variables, auth modes (Off, Local accounts, Cloudflare Access) and paired
  devices all keep working.
- The repository is public: no host names, addresses or names of people in
  code, tests or commits.
- Signing needs: Apple Developer ID Application certificate (the team already
  exists) for macOS; a Windows code signing certificate for Windows.
- No emoji or em dashes in UI text or docs. Say "TV", "Movies", "series".
- Every user-facing step is covered by a clicking test: Playwright for the web
  pages, XCUITest for the apps. Installers get a smoke test in CI that
  installs, starts, and asks `/api/tv/hello`.

## Decided by the owner

- One-time $9.99 universal purchase for the apps; the server stays free.
- Everything in the first-ten-minutes picture above.
- No relay service.
- Order: installers, Plex sign-in, discovery, away from home.

## Calls made in design (check these)

- **Away from home needs a password.** The switch refuses to turn on while any
  way in from outside has no password: sign-in Off, or Local accounts with
  an account that has none. It offers to switch to Local accounts and make
  the first account there and then. With Cloudflare Access, Cloudflare does
  the sign-in and the switch adds the rest (limits, headers, HTTPS when
  wanted). An upgraded install with sign-in Off is unchanged, because the
  switch starts off.
- **Library folders are suggested, never assumed.** The server reads the
  library paths from Plex, checks it can write to each by making and
  removing a test file, and shows them filled in for the owner to confirm.
  Nothing is written into a Plex folder before that confirm.
- **The apps pin the public key, not the whole certificate.** The server's
  own certificate lasts ten years and lives in the data folder, so it
  survives restarts, updates and address changes. A reinstall without the
  old data makes a new key; the apps then say so and the owner pairs again
  at home. A certificate a browser would trust (a real domain) is accepted
  as well as the pin. Each device keeps its own pin.
- **First pairing happens at home**, by tapping the server or scanning the QR
  code (the QR also carries the key's fingerprint, which is not a secret).
  Pairing from outside is allowed only to a server with a real certificate.
  At home the pairing endpoints stay as they are.
- **Supported systems**: Windows 10 and 11 on x64; macOS 13 and later, Apple
  silicon and Intel; Linux amd64 and arm64, with `.deb` tested on Debian 12
  and Ubuntu 22.04 and later, `.rpm` on Fedora and RHEL 9 and later. The
  bundled ffmpeg is a static build with each system's hardware decoding
  (VA-API, VideoToolbox, D3D11/QSV), which is used when present.
- **Updates**: the server says an update is available. On Windows the one
  click downloads the installer from the GitHub release, checks its SHA-256
  against the release's checksum file (the installer is unsigned for now),
  and runs it. On macOS the app checks the new app's Developer ID signature
  and replaces itself (Sparkle expects a native Cocoa app; this one is
  Python). Linux updates through its package manager or `install.sh`.
- **One port for HTTP and HTTPS**: with Use away from home on, the port
  answers both, telling them apart by the first byte of each connection, so
  home browsers are not sent to a certificate warning.
- **Video away from home in the apps**: AVPlayer cannot trust a pinned
  certificate, so with the server's own certificate the apps play through a
  small proxy inside the app on 127.0.0.1. With a real domain certificate
  the proxy is not used. AirPlay away from home needs the real certificate.
- **The download page** is a static site on Cloudflare Pages at
  couchelephant.app (registered 2026-11-18). Windows ships unsigned first.
- **A second copy on the same machine**: the installer checks the port first
  and offers the next free one. Each install has its own id, so the apps list
  both. Moving from Docker uses the existing export and import.
- **The apps list each server once**, by its install id, remember the last
  address that worked, and fall back to it when Bonjour finds nothing
  (guest Wi-Fi, separate networks). Typing stays.

## Left to the builder

- The freezing tool, installer tool (for example Inno Setup or WiX on
  Windows) and package tooling on Linux.
- Exact settings layout, wording and the look of the setup page, within the
  app's existing style.
- Whether macOS ships one universal download or two.
- The Bonjour library on the server and its TXT record fields.
