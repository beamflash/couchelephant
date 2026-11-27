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

An app on the internet is "away from home", and the apps do not take a typed
password. They show a six letter code and wait for somebody to approve it. At
home that is done in the web Settings, but Settings is shut to anyone outside
the home network, so the demo has its own page at `/demo` that takes the code
the app shows plus the review code. The review code is `DEMO_CODE`, set in the
server's environment and never in the source.

The review code is also the web password for the user `reviewer`, if the
reviewer wants to look at the web page at `https://demo.couchelephant.app`.

## Reviewer notes for App Store Connect

Paste this into App Review Information, Notes. Put the real review code where it
says so. Nothing else is needed: there is no sign-in field to fill in, so leave
the user name and password boxes empty and tick "Sign-in required" off.

```
CouchElephant is the client for a self-hosted TV recorder. It needs a server,
so we run a public demo server for review at:

    https://demo.couchelephant.app

Everything on it (channels, shows, recordings, pictures) is invented. It resets
every night, and it is safe to record, change channels and delete from the
guide as much as you like.

REVIEW CODE: <put the DEMO_CODE here>

To sign in, on iPhone or iPad:
1. Open CouchElephant. In the address box type  https://demo.couchelephant.app
   (with the https://) and tap Connect.
2. The app shows a six letter code ("Let this phone in").
3. In Safari (on the same phone, or on any computer) open
   https://demo.couchelephant.app/demo
   Type the review code above and the six letters, then tap "Let the app in".
4. Go back to CouchElephant. It comes in by itself within a few seconds.

On Apple TV, do the same: type https://demo.couchelephant.app with the on-screen
keyboard, read the six letters off the TV, and approve them from a phone or
computer at https://demo.couchelephant.app/demo.

Things to try:
- Home: "The Long Way Home" is left at 0:55. Press play: it resumes, and a
  "Skip ad" button appears at 1:10 for the ad break.
- Guide: open any channel to watch live TV (a moving test pattern). While it
  plays, press down on the Apple TV remote, or swipe up on the iPhone, for the
  guide over the picture and change channel.
- Guide: open a programme and record it; it shows under Recordings, Set to record.
- Recordings: Set to record has the scheduled recordings and a season pass.
- Settings: Report an issue works as it does in the shipping app.

The demo allows two video streams at once. If a live channel says every tuner is
in use, wait a minute and try again.
```

Wording to change if the first review comes back asking for more: Apple wants
working credentials, so the notes say plainly why there is no user name field.

## Putting it on a server

A fresh Ubuntu 22.04 or 24.04 VPS with 1 to 2 vCPU and 2 GB, x86_64 or arm64.
This is known to work on a Google Cloud e2-small (2 shared vCPU, 2 GB, x86_64,
Ubuntu 24.04) with the DNS record not proxied, so Caddy's Let's Encrypt check
reaches the server directly. A shared-core machine like that has a low sustained
CPU allowance and bursts above it on credits, so set `DEMO_TUNERS` to 1 in the
compose file if it ever runs short.

1. Point DNS at it. Make an `A` record for `demo` (and an `AAAA` record if the
   server has IPv6) in the `couchelephant.app` zone, with the server's address.
   Caddy cannot get a certificate until this resolves, so check
   `dig +short demo.couchelephant.app` from your own computer first.
2. Open ports 80, 443 and 8710 (TCP) and 443 (UDP, for HTTP/3). With `ufw`:

   ```
   sudo ufw allow OpenSSH
   sudo ufw allow 80,443,8710/tcp
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

   `openssl rand -base64 18` makes a good one. It is at least 12 characters, and
   it is also the web password for `reviewer`.

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

## Resetting by hand

The demo puts itself back to the seeded state when it starts and at 04:00 every
night on the container's clock (`TZ`, Pacific by default). Nobody has to do
anything. To reset it right now:

```
docker compose restart demo
```

Everything a visitor made (devices that were let in, bookings, passes, deleted
or watched positions, favorites) is gone after that, and so is every web
session. To throw away even the data volume: `docker compose down -v`, then
`docker compose up -d`.

## What it does not let anyone do

Sign-in is on and the demo is "away from home" mode, so a visitor from the
internet reaches only what a paired app or the signed-in `reviewer` may.

- No test routes. There is no `/test/...` anything, and no fake Firestore.
- Writes are on an allow list (`app/demo.py`). The apps' own use is let
  through: favorites, recording, passes, live TV, resume positions. Everything
  else answers 403: Settings, accounts, devices, backups, import and export,
  updates, the Plex and tuner addresses and every button that makes the server
  fetch an address. Deleting a recording is off too.
- Reports are never sent from the server. The web page answers "This is a demo,
  so your report was not sent anywhere." See the first item below for the apps.
- The sign-in limits stay on: five wrong passwords or review codes in ten
  minutes lock that address for a minute, doubling up to an hour, and asking for
  pairing codes counts the same way. The address is the real visitor's, read
  from Caddy's `X-Forwarded-For`, and only Caddy is believed.
- At most two video streams at once (the fake tuner has two tuners), every idle
  live stream is stopped after a minute, and the container has a memory limit.

## What could not be locked down

- **A report sent from an app goes to the owner's real Firestore.** The apps
  send their reports straight to it, not through the server, and a build in
  review cannot be told otherwise. The server part of such a report carries
  `"demo": true`, so it can be told apart and thrown away. The web page's
  Report an issue is blocked as described above.
- **The plain HTTP port 8710.** It is there so an address typed without
  `https://` still works (the apps turn a bare name into `http://name:8710`).
  The device key crosses the internet unencrypted on it. The key is worth nothing
  outside this demo. Delete the second block of `deploy/demo/Caddyfile` to turn
  it off.
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
"Every tuner is in use". Live sessions nobody touches for five minutes are ended.
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
