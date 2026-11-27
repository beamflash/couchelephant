# The public demo

A copy of CouchElephant that anyone with the review code can sign in to from the
iPhone and Apple TV apps, with nothing real in it. It exists for Apple's App
Store reviewer, who has no tuner, no Plex and no home network to point the apps
at. It runs at `https://demo.couchelephant.app`.

Everything on it is invented: the networks (Maple TV, Heron TV, Cedar TV and so
on), the shows, the teams and the films. The test pattern on a live channel is
made by ffmpeg, and the channel logos are drawn by the server. It resets itself
every night.

## What runs

One container (`Dockerfile.demo`) and Caddy in front of it for HTTPS.

- The app, the same code as the shipping image, with demo mode switched on
  (`COUCHELEPHANT_DEMO=1`, see `app/demo.py`).
- The invented world the Apple tests use (`tests/demo_world.py`): a fake Plex
  holding an invented week and a half of guide, a fake HDHomeRun that sends a
  moving test pattern on all eight channels, and the finished recordings.
- `scripts/demo_server.py` starts it all. It is a separate entry point, not a
  mode of the normal image, because the fakes live in `tests/` and the shipping
  image carries none of it. The app-side policy (the lock, the pairing page,
  reports) is ordinary code in `app/demo.py` that does nothing until the switch
  is on.

What is in it:

| What | Where to find it |
| --- | --- |
| A guide for the next day and a half, eight channels, news every six hours | Guide tab |
| Live channels, each a different colour of test pattern | Guide, open a channel |
| Six finished recordings | Home, Recent, and the Recordings tab |
| One 20 minute recording ("The Long Way Home") with three ad breaks, left at 0:55 | Home, Recent: it resumes, and Skip ad shows at 1:10 |
| One film left part way through ("Bend in the Willow") | Home, Recent |
| Two one-off bookings and a season pass ("Marrowbrook") | Recordings, Set to record |

## How the reviewer signs in

The apps ask for an access code instead of the usual six letters to approve:

1. Open the app and type `demo.couchelephant.app`.
2. The app shows one field, "Access code". Type the code.
3. It is in.

The access code is `DEMO_CODE`, set in the server's environment and never in the
source. It is 12 to 16 lowercase letters and digits with no look-alikes (no 0,
o, 1, l or i), so it can be typed with a TV remote; the server will not start
with any other kind. It is also the web password for the user `reviewer`.

How it works: when the server is the demo, `GET /api/tv/hello` also says
`"code_signin": true`, and the apps then show the field and send the code to
`POST /api/tv/pair/code`. A right code makes a paired device and answers its
key, as an approved pairing does. A wrong one makes nothing and counts against
the sender's address: the access code has its own budget of 20 wrong tries per
address in ten minutes (an office on one address may mistype now and then, and a
right code always works inside the budget; the code is far too long to guess in
20 tries). Past it even the right code waits, except that an address which has entered the
right code keeps being let in with that exact code for 24 hours (up to 60 tries
in ten minutes), so a mistyping colleague cannot lock the reviewer out. On a normal install that route answers 404 and `hello` never
says `code_signin`. The older way still works too: the app's six letter code
can be approved with the review code at `/demo`.

A name typed on its own (a dot in it, not `.local`, not an IP address) is tried
over `https://` first and only then as `http://name:8710`, so
`demo.couchelephant.app` reaches the demo over HTTPS with no scheme typed. The
demo is only reachable over HTTPS: the Caddyfile has no plain-HTTP block, and
the access code never crosses the internet unencrypted.

## Reviewer notes for App Store Connect

Paste this into App Review Information, Notes. Put the real access code where it
says so. There is no user name or password field to fill in, so leave those
boxes empty and "Sign-in required" off.

```
CouchElephant is the client for a self-hosted TV recorder, so we run a public
demo server for review. Everything on it (channels, shows, recordings, pictures)
is invented, and it resets every night.

ACCESS CODE: <put the DEMO_CODE here>

iPhone / iPad:
1. Open CouchElephant and type  demo.couchelephant.app  in the address box, then
   tap Connect.
2. Type the access code in the "Access code" field and tap Sign in.
3. You are in.

Apple TV:
1. Open CouchElephant and type  demo.couchelephant.app  (Select on the box,
   type on the remote's keyboard, then choose Connect).
2. Select the "Access code" field, type the code, then choose Sign in.
3. You are in.

Things to try:
- Live TV: Guide, open a channel (a moving test pattern). While it plays, press
  down on the Apple TV remote, or swipe up on the iPhone, for the guide over the
  picture and change channel.
- Guide: look through the next day, open a programme, and record it.
- Home: "The Long Way Home" is left at 0:55. Play it: it resumes, and a "Skip ad"
  button shows at 1:10.
- Recordings: Set to record has the scheduled recordings and a season pass
  ("Marrowbrook").
- Search: the magnifier in the Guide (try "lantern" or a channel number).
- Settings: Report an issue opens the report screen as in the shipping app.

The demo allows two video streams at once. If a live channel says every tuner is
in use, wait a minute and try again.
```

## Putting it on a server

A fresh Ubuntu 22.04 or 24.04 VPS with 1 to 2 vCPU and 2 GB, x86_64 or arm64.
This is known to work on a Google Cloud e2-small (2 shared vCPU, 2 GB, x86_64,
Ubuntu 24.04) with the DNS record not proxied, so Caddy's Let's Encrypt check
reaches the server directly. A shared-core machine like that has a low sustained
CPU allowance and bursts above it on credits, so set `DEMO_TUNERS` to 1 in the
compose file if it ever runs short.

1. Point DNS at it. Make an `A` record for `demo` in the `couchelephant.app` zone, with the server's address.
   Caddy cannot get a certificate until this resolves, so check
   `dig +short demo.couchelephant.app` from your own computer first.
2. Open ports 80 and 443 (TCP) and 443 (UDP, for HTTP/3). With `ufw`:

   ```
   sudo ufw allow OpenSSH
   sudo ufw allow 80,443/tcp
   sudo ufw allow 443/udp
   sudo ufw enable
   ```

3. Install Docker:

   ```
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER      # log out and in again
   ```

4. Get the code and set the review code:

   ```
   git clone https://github.com/beamflash/couchelephant.git
   cd couchelephant/deploy/demo
   cp .env.example .env
   nano .env            # set DEMO_CODE to a long random string
   ```

   It must be 12 to 16 lowercase letters and digits without 0, o, 1, l or i.
   This makes one:
   `python3 -c "import secrets;print(''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(14)))"`

5. Start it:

   ```
   docker compose up -d --build
   ```

   The first build makes the viewing copies of the recordings (a few minutes of
   ffmpeg) and downloads Caddy. Start-up after that takes a few seconds.

6. Check it:

   ```
   curl -s https://demo.couchelephant.app/healthz
   curl -s https://demo.couchelephant.app/api/tv/hello
   ```

   `hello` should say `"pairing_required": true`. Then do the reviewer's steps
   yourself from a phone, once, before submitting.

To update: `git pull`, then `docker compose up -d --build` again.

## Resetting

The content (the guide, the recordings, bookings, passes, favorites, the
settings) goes back to the seeded state when the demo starts and at 04:00 every
night on the container's clock (`TZ`, Pacific by default). Nobody has to do
anything. Two things are kept across a reset on purpose, so a reviewer is not
thrown out: the apps that were let in (and their keys), the web sessions, and the server's
identity (its install id, so the apps do not say "not the server you paired with").

The nightly reset is skipped if anyone with a key or a session was seen in the
last 30 minutes. It asks again every hour, but never holds out past three nights:
then it goes ahead (devices, sessions and the server's identity are still kept). While a reset runs (a few seconds to
a minute) every request but `/healthz` answers 503, and the sign-in settings are
rewritten in place, never cleared, so the demo is never open for a moment.

To reset the content right now: `docker compose restart demo`. To forget the
apps that were let in as well, throw away the data volume with
`docker compose down -v`, then `docker compose up -d`.

## What it does not let anyone do

Sign-in is on and the demo is "away from home" mode, so a visitor from the
internet reaches only what a paired app or the signed-in `reviewer` may.

- No test routes. There is no `/test/...` anything, and no fake Firestore.
- Writes are on an allow list (`app/demo.py`). The apps' own use is let
  through: favorites, recording, passes, live TV, resume positions. Everything
  else answers 403: accounts, devices, backups, import and export, updates and
  every button that makes the server fetch an address. Deleting a recording is
  off too. The web Settings pages send a visitor to a short page at `/demo` that
  says what to do. What an app's Settings screen reads has the server's own
  addresses left out.
- Reports are never sent from the server. The web page answers "This is a demo,
  so your report was not sent anywhere." See the first item below for the apps.
- The sign-in limits stay on: five wrong web passwords in ten minutes lock that
  address and account for a minute, doubling up to an hour, and asking for
  pairing codes counts the same way. The review and access code has its own
  budget (above). The address is the real visitor's, read from Caddy's
  `X-Forwarded-For`, and only Caddy (a fixed address, 172.29.0.2, trusted as a
  single address) is believed.
- Nothing opens without the code, the web session cookie is marked Secure, and
  the sign-in settings cannot be unset by anything the database holds.
- At most two video streams at once (the fake tuner has two tuners). A live
  channel stops a minute after the app stops playing it, five minutes after a
  pause, and 30 minutes at the very most however it is kept alive.
- Disk: a visitor can have two bookings at a time, a recording they make stops
  after 30 minutes, the recordings folder is capped at 3 GB (the oldest recording
  a visitor made goes first; the seeded ones never do), and the container has a
  memory limit. The server this is meant for has a 20 GB disk.

## What could not be locked down

- **A report sent from an app goes to the owner's real Firestore.** The apps
  send their reports straight to it, not through the server, and a build in
  review cannot be told otherwise. The server part of such a report carries
  `"demo": true`, so it can be told apart and thrown away. The web page's
  Report an issue is blocked as described above.
- **There is no per-address cap on connections** the way a home install has,
  because every visitor arrives from Caddy. Caddy's own open-source build has no
  rate limit either. A flood of requests costs CPU, and the container's memory
  limit stops it taking the machine. Use the VPS provider's firewall if it is
  ever hit.
- **Anyone who has the review code can use the demo.** There is nothing to
  steal in it, and it resets every night. Change `DEMO_CODE` in `.env` and run
  `docker compose up -d` to change the code.
- **The viewing copies of recordings are real video** that anyone signed in can
  download. They are test patterns.

## How much it costs the server

Measured with the demo running directly on a busy development machine, CPU time
of the server and every ffmpeg it started, over 30 seconds:

| What is running | CPU used |
| --- | --- |
| Idle (the guide, signed in, nothing playing) | about 0.01 of one core |
| One live channel being watched | 0.42 of one core (0.10 for the test pattern, 0.31 for the viewing copy, 0.01 for the server) |
| One live channel and one finished recording playing | 0.41 of one core (playing a recording is only serving files) |
| Two live channels (the most the demo allows) | about 0.85 of one core |

The viewing copy is software x264 at 640x360, 24 frames a second. The fake
tuner has two tuners (`DEMO_TUNERS`), and a recording that starts on schedule
takes one, so at most two ffmpeg pairs ever run. A third request is answered
"Every tuner is in use". A live channel stops a minute after the app stops
playing it, and five minutes after a pause.
On disk the demo is about 200 MB of recordings plus the guide.

## Behind a proxy: what the apps expect

The apps check a server's certificate with the system's own trust, so a real
Let's Encrypt certificate from Caddy is accepted with no pin stored. A pin is
only for a server that signs its own certificate.

Two things the server had to learn for this, both in demo mode only:

- Caddy speaks plain HTTP to the container. Without help the server believed it
  was on `http://` and handed the apps `http://` links to pictures and logos,
  which the apps refuse to send their key to (a different origin from the
  `https://` one they stored), so every picture was blank. The demo takes the
  scheme from `X-Forwarded-Proto`, from the one trusted proxy only.
- The away rule "pair at home first" lets an outsider pair only over HTTPS to a
  domain whose certificate the server itself holds. Caddy holds this one, so the
  demo lets pairing through and gates it with the review code instead.

## Trying it on a LAN without a domain

Set `DEMO_HOST` in `.env` to a name that points at the machine (for example
`192.168.1.20.sslip.io`) and `CADDYFILE` to a Caddyfile that says `tls internal`
for it. Caddy then makes its own certificate, and its root, in the `caddy-data`
volume at `caddy/pki/authorities/local/root.crt`, can be trusted by a test
device (`xcrun simctl keychain <device> add-root-cert root.crt` for a simulator).
The apps only send the access code to `https://` addresses, so a plain-http demo
cannot be used for the sign-in.
