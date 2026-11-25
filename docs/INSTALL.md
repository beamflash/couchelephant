# Install and configure

## What you need

- A Plex Media Server with a DVR set up and a channel lineup.
- Either the installer for your system from [couchelephant.app](https://couchelephant.app), or Docker on any machine that can reach that server.

CouchElephant does not need to run on the same machine as Plex, and it does not
touch your media files.

## Install it

Get the installer for Windows, macOS or Linux from
[couchelephant.app](https://couchelephant.app). On Linux, this picks the right
package for you:

```bash
curl -fsSL https://couchelephant.app/install.sh | sudo sh
```

## Run it with Docker

```bash
docker run -d --name couchelephant --restart unless-stopped \
  -p 8710:8710 \
  -v /opt/couchelephant/data:/data \
  -e TZ=UTC \
  ghcr.io/beamflash/couchelephant:latest
```

The image is built for `linux/amd64` and `linux/arm64`, so the same tag works
on an ordinary server and on a Pi. Pin a version if you would rather choose
when to move: `:1` follows fixes within a major version, `:1.2.3` never moves.

The same image is on Docker Hub as `datbird/couchelephant`. Either address
works; GHCR is the one to prefer, because Docker Hub allows only 100
anonymous pulls per address per six hours and everyone behind your router
shares that count.

There is a `docker-compose.yml` in the repository that does the same thing.

**Letting the apps find it.** On Docker's default bridge network the container
cannot announce itself on your home network, so the apps will not list it; it
says so once in its log. Either type its address in the app, or run it with
host networking (`--network host` in place of `-p 8710:8710`), and the apps
list it by name.

### On Unraid

Search for **CouchElephant** in **Apps**. The template sets the port to 8710
and the appdata path to `/mnt/user/appdata/couchelephant`, which is all it
needs. For the apps to list it by name, set **Network Type** to **Host**;
on the default bridge, type its address in the app. Everything else is configured in the app itself.

If you use CouchElephant DVR, add two variables, **PUID** and **PGID**, and set
them to the user and group Plex runs as (on Unraid usually `99` and `100`).
Plex makes each show's folder as its own user, and CouchElephant can write
into whatever Plex makes only when it runs as that same user. Settings,
Recording says which user made your library's folders. If a folder still
blocks a recording, CouchElephant tells you which one and gives the command to
run. On Unraid, **Tools, New Permissions** on the share fixes every folder at
once.

The template lives in
[datbird/unraid-templates](https://github.com/datbird/unraid-templates), which
is what Community Applications reads.

### From source

Clone the repository and run `docker build -t couchelephant .`, then use
`couchelephant` in place of the image name above.

Everything it keeps lives in the one volume, so that is the only thing to back
up.

| Path in the volume | What it holds |
| --- | --- |
| `couchelephant.db` | The guide cache, rules, and the decision log |
| `auth.db` | Accounts and sessions, kept apart so rebuilding the cache cannot take them |
| `logos/` | Cached channel logos, and any you supplied yourself |

## Environment

| Variable | Default | What it does |
| --- | --- | --- |
| `COUCHELEPHANT_DB` | `/data/couchelephant.db` | The main database |
| `COUCHELEPHANT_AUTH_DB` | next to the main one | Accounts and sessions |
| `COUCHELEPHANT_LOGOS` | `/data/logos` | Cached channel artwork |
| `COUCHELEPHANT_RECORDINGS` | `/recordings` | Where CouchElephant DVR keeps its files. It is off unless this folder exists |
| `COUCHELEPHANT_LIBRARY` | `/library` | The Plex library folder a finished recording is moved into |
| `COUCHELEPHANT_ADDRESS` | none | The address of this machine on your home network, such as `192.168.1.20`. The QR code under Settings carries it. Set it when the container is on Docker's bridge network, where it cannot see that address itself. With **Use away from home** on, the QR code is an `https://` address |
| `PUID`, `PGID` | none | Docker only. Run CouchElephant as this user and group, so it can write the folders Plex makes in your library. Set them to the user Plex runs as (on Unraid, usually `99` and `100`). Without them the container runs as root, as before. `PGID` defaults to `PUID`. The user keeps the group of every `/dev/dri` device, so hardware encoding still works CouchElephant takes over only `/data` and `/recordings`, never the library |
| `TZ` | `UTC` | The container's clock |

Everything else is a setting in the app, not an environment variable, because
it is the kind of thing you change while looking at the result.

## CouchElephant DVR

CouchElephant DVR is CouchElephant recording from your tuner itself, next to
Plex DVR. It is optional. Skip this and Plex makes every recording, as it
always has.

Turn it on to watch a recording from its start while it is still being made.
See [Who makes the recording](RECORDING.md#who-makes-the-recording) for what
it does. It needs an HDHomeRun tuner, which is the tuner Plex already uses.

```bash
docker run -d --name couchelephant --restart unless-stopped \
  -p 8710:8710 \
  -v /opt/couchelephant/data:/data \
  -v /opt/couchelephant/recordings:/recordings \
  -v /path/to/your/plex/media:/library \
  --device /dev/dri \
  -e TZ=UTC \
  ghcr.io/beamflash/couchelephant:latest
```

| Addition | What it is for |
| --- | --- |
| `/recordings` | Where a recording is held until Plex has it, and the copies you watch. A local disk. Allow about 4 GB for each hour held. **Mounting this is what turns the recorder on** |
| `/library` | The folder your Plex library reads, so a finished recording can be moved into it. Without it recordings are kept in `/recordings` and not moved |
| `--device /dev/dri` | Lets an Intel or AMD graphics chip make the copy you watch. Without it the processor does the work, which a small machine may not manage for more than one recording at a time |

The container must be able to write to both folders.

Then open **Settings, Recording, CouchElephant DVR**. It says whether the recorder
can run and why not, shows the tuner it found, and asks two things for the
move into Plex: which folder inside `/library` takes shows and which takes
films, and which Plex library each belongs to. The tuner address is read from
Plex. Type one only if that fails.

A station sends MPEG-2 video, which a television app and most browsers cannot
play. So each recording has two files: the broadcast, untouched, which is what
Plex receives, and a copy made for watching (H.264, stereo). The copy is kept
for two days after Plex has the recording and is made again on demand.

### The Apple TV and iPhone apps

The Apple TV app and the iPhone and iPad app play what CouchElephant
records, tune live channels, show the guide, and record from it. They are
not in this repository; they come from Apple's TestFlight for now.
The first run lists every CouchElephant on your home network by name; pick
yours. If none shows after a few seconds, or the app was not allowed to look
for devices on your network, type the address instead. You can also scan the
QR code under **Settings, Recording, Televisions** with an iPhone or iPad
camera. The server announces itself unless you turn off **Let the apps find
this server on the home network** in that same place. If the server's address
changes later, a paired app finds it again by itself. With sign-in on the
app shows a six-letter code, and you type that code under **Settings,
Recording, Televisions** to let that device in. A phone is listed there too,
with the same per-device settings as a television.

The **Home** tab in the web app shows the same three lists and plays in a
browser.

## Point it at Plex

Open `http://your-host:8710`. The welcome page, and later **Settings, Plex**,
has a **Sign in with Plex** button.

1. Press **Sign in with Plex**. A four-character code shows on the page.
2. Open [plex.tv/link](https://plex.tv/link), sign in to Plex if it asks, and
   type the code.
3. Back on the page, pick your Plex server by name if you have more than one.
   Only servers your account owns are listed, so sign in with the account that
   runs the server, not one that was shared with you.

CouchElephant tries each address Plex lists for that server, nearest first
(for a local server, Plex's `plex.direct` name and then the plain address),
and keeps the first one that answers and has a DVR. It never saves a relay
address: if only Plex's relay answers, it says so and you can enter the
address yourself. If none answers it names every address it tried. The code runs out after about 15 minutes; press the
button again for a new one. On a new install this also turns on Preview mode.

### Enter it yourself

If plex.tv cannot be reached, or you would rather not sign in, type the address
and token. On the welcome page, press the **Enter it yourself** link. In
**Settings, Plex** the address and token form is already shown.

**Address.** The address must work from inside the container. `127.0.0.1` only
works if Plex is running in that same container, which it is not, so use the
machine's real address.

**Token.** On the Plex server this is the `PlexOnlineToken` in
`Preferences.xml`. It is kept in the local database and never written to a log.
The field shows dots once one is stored; leave them alone to keep the current
token.

Press **Test connection**. It checks three separate things and tells you which
one failed:

| What it says | What to do |
| --- | --- |
| No server address set | Fill in the address |
| No token set | Paste the token |
| Could not reach *address* | Wrong host, wrong port, or nothing listening. Remember it must resolve from inside the container |
| The server answered but rejected the token | The token is wrong or expired |
| Reached it, but it has no DVR | Add a tuner to Plex first |

## Timezone

Under **Settings, Plex, Guide**. Every time in the app is shown in this zone.
The guide itself is stored as UTC timestamps, so changing this never moves a
recording.

## Recording folders

Under **Settings, Recording, CouchElephant DVR**, CouchElephant suggests the TV
and Movies folders from your Plex libraries, and checks it can write to each.
Nothing is saved until you press **Use these folders**. A folder that belongs
to another computer, such as Plex on a NAS, shows why it cannot be used here;
type a folder this computer can reach instead.

## Preview mode

On for a new install, under **Settings, Recording**.

With it on, rules work out which airing they would choose and show it to you,
but nothing is written to your DVR. It exists so you can watch CouchElephant
make its choices before you trust it with your server. Turn it off when the
choices look right.

## Alerts

Off for a new install. **Settings, Notifications.**

Nothing is sent until you add a destination. Add as many as you like, name each
one, and tick which events it carries, so faults can go to one channel and
recordings to another.

| Where | What you need | How to get it |
| --- | --- | --- |
| **Discord** | a webhook URL | Channel settings, Integrations, Webhooks, New Webhook, Copy URL. No bot, no application, no OAuth |
| **Telegram** | a bot token, and a chat | Ask `@BotFather` for a token. Paste it, save, message your bot once, then press **Find chat** and it reads the id for you |
| **Notifiarr** | your **global** API key, and a Discord channel id | The key is on your Notifiarr account. Integration-specific keys are rejected by the passthrough. For the channel: turn on Developer Mode in Discord, right-click the channel, Copy Channel ID |
| **Webhook (JSON)** | the full URL, and optionally a bearer token | For your own script, relay, Home Assistant or n8n. The URL looks like `http://192.168.1.10:8791/notify`. It needs no channel: it is handed the severity and picks the channel itself. One POST per message, with `Authorization: Bearer <token>` only when a token is set. Severity is `ok`, `warn` or `bad`. Any 2xx answer counts as delivered, unless the reply is JSON with `"ok": false` |

All four are outbound only. Nothing listens, nothing polls, and nothing here
ever needs a port forwarded, which matters on a home server with no public
address.

**Press Send a test on each one before you trust it.** The verdict says what
happened rather than only that it failed.

Both the Discord webhook URL and the Telegram, Notifiarr or webhook token are kept
like passwords: masked in the page and never written to a log. A Discord webhook
URL is a bearer credential, so anyone holding it can post into your channel. A
webhook's address is not a credential, so it is shown back to you rather than
masked.

**A webhook picks the channel, not CouchElephant.** Every message carries its
severity, and your receiver can route on it: a fault goes to the channel you watch and
everything else, a recording that started, a pass that booked something, a fault
that cleared, goes to the channel you read later. Nothing has to be configured
twice, and this end never learns which channels exist.

A fault is announced when it opens, mentioned again on the interval you choose
while it stays open, and announced once more when it clears. A one-off event,
like a pass booking a recording, is said exactly once however many syncs run.
A destination added today is not sent a backlog of what is already open.

Suggestions are never sent. Only a fault, or something that actually happened,
is worth interrupting you for.

## Use it away from home

Not needed if you reach the box over a VPN or an existing tunnel. Leave
**Use away from home** off and nothing below applies.

To open it to the internet, see [AUTH.md](AUTH.md) first. Then, on your
router, forward:

- **The CouchElephant port** (8710 unless you changed it) to this computer.
  Plain HTTP and HTTPS both arrive here.
- **Port 443** to this computer, only if you want a real certificate for a
  domain. Let's Encrypt must reach the same CouchElephant port from the
  outside on 443, so forward external 443 to the CouchElephant port, for
  example 443 to 8710. No port 80 is needed.

Give the computer a fixed address on your router so the forward keeps working.

## Sign-in

Off for a new install. See [AUTH.md](AUTH.md).

Turn it on before this is reachable from anywhere but your own network.

## Sync

The guide refreshes on a timer, default 60 minutes, set under **Settings, Plex,
Guide**. Each sync pulls channels, programmes, airings, teams and Plex's own
recordings, then re-runs every pass. The sync icon in the header runs one now,
and its tooltip carries the last result.
