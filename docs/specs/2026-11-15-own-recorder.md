# CouchElephant records it itself, and an Apple TV app plays it

Written 2026-11-15. Every decision below is settled unless it is marked
**builder's call**, which means it was chosen during the build and is open to
change. This is a build document, not an options paper.

## Why this exists

Come home an hour into a football game that Plex is recording. On the Apple TV,
Plex offers the live channel and nothing else. There is no way to start at the
kickoff until the recording has ended.

That fault is on the Plex server, so no other Plex client fixes it. Jellyfin has
the same fault. Channels DVR does it correctly and costs $8 a month.

CouchElephant already owns the guide, the passes, the schedule and the choice of
the live airing. The one thing it does not own is the recording. This adds that,
as an option, and adds a client that can play a recording while it is still
being made.

## What does not change

A person who never turns this on sees no difference. Plex still makes every
recording, and CouchElephant still only chooses which airing Plex takes. That
stays the default.

## The choice: "Recorded by"

Every pass and every single recording carries one new choice:

| Value | Who records |
| --- | --- |
| Plex (default) | Plex, pinned by CouchElephant, exactly as today |
| CouchElephant | CouchElephant, from the tuner, itself |

A setting, `recorder_default`, picks the value a new pass or recording starts
with. Setting it to CouchElephant is how "CouchElephant records everything" is
said. There is no separate mode.

The two recorders share the tuner. An airing is booked with one of them, never
both.

## The life of a CouchElephant recording

| State | What is true |
| --- | --- |
| `scheduled` | A row exists. Nothing is running |
| `recording` | The tuner is open. The broadcast file and the viewing copy both grow |
| `finished` | The broadcast ended. Both files are complete and still held here |
| `moving` | The broadcast file is being put into the Plex library |
| `in_plex` | Plex has it. CouchElephant still lists it and can still play it |
| `failed` | It did not record, with a sentence that says why |

### Two files, on purpose

**The broadcast file** is the stream exactly as the tuner sent it, written to
disk untouched. This is what Plex receives. It is the same thing Plex would have
recorded itself.

**The viewing copy** is an HLS playlist made from the broadcast file by a second
process. The Apple player cannot decode the MPEG-2 video that stations send, so
this copy is H.264 with AAC audio. It is disposable.

They are two processes so that the recording cannot be hurt by the conversion.
If the converter falls behind or dies, the broadcast file is still whole and the
converter starts again from where it stopped. Measured on the dev host: the
tuner sends about 1.25 MB a second for a 1080i channel, and the software
converter keeps up at 1.04x.

The playlist is an HLS **event** playlist. It only ever grows, it never drops an
old segment, and the Apple player treats it as "start anywhere from the
beginning up to live". That one property is the whole feature. When the
recording ends the playlist is closed and becomes an ordinary finished video.

### Handing over to Plex

After a recording finishes, and only when nobody is watching it:

1. State becomes `moving`. The list shows a "Moving to Plex" badge.
2. The broadcast file is copied into the Plex library folder under the name Plex
   would have given it, and the copy is checked by size.
3. Plex is asked to scan that folder, and CouchElephant waits until Plex lists
   the file.
4. The resume position is written to Plex, so a game stopped in the third
   quarter here resumes there in Plex.
5. State becomes `in_plex`. Only now is the held broadcast file removed.

**The recording stays playable the whole time.** The viewing copy is not touched
by the move, so the badge is information and not a lock. If somebody starts
watching during the move, nothing is taken away from them.

"Nobody is watching" means no client has reported playback of that recording for
three minutes. A paused player still reports, so a pause holds the handover.

After the handover the viewing copy is kept for two days, then removed.
Playing an `in_plex` recording after that makes a new viewing copy from the Plex
file, on demand. **Builder's call:** two days, as a setting.

A handover that fails leaves the recording `finished` and raises a notice. It
is tried again on the next pass of the loop. Nothing is deleted until Plex has
the file.

### One position, for now

The resume position is one per recording, not one per person. Plex has a
documented call that sets it for the account whose token CouchElephant holds. A
position for each family member needs each person's Plex sign-in and waits for a
later version.

## What CouchElephant shows for Plex's own recordings

CouchElephant lists every recording in progress, from both recorders, so it is
the one place to look for live television.

A recording Plex is making is listed as status only, with the words "Cannot play
while Plex is recording". Plex writes into a hidden `.grab` folder and moves the
file when the recording ends. A second reader on that file risks the move, and
this project does not put a Plex recording at risk to save a click. The row
offers one action: record the next airing with CouchElephant instead.

## The tuner

Version one supports HDHomeRun, because that is what a tuner speaks without a
driver: a channel is `http://<tuner>:5004/auto/v<channel>` and the stream is
whatever arrives.

- The tuner address comes from Plex's own DVR device list, so there is nothing
  to type. A setting overrides it.
- `GET /status.json` on the tuner says which tuners are busy, including the ones
  Plex holds. CouchElephant reads it before it opens one.
- **A recording outranks live viewing.** If a recording is due and every tuner is
  busy, a live viewing session CouchElephant itself started is closed to free
  one. A tuner Plex holds is never taken. **Builder's call.**
- Watching a channel that is already being recorded uses the recording. It does
  not open a second tuner.
- ATSC 3.0 channels are refused in version one. Their audio is AC-4, which
  ffmpeg does not decode, and most of them are encrypted. Every one of those
  stations has an ATSC 1.0 twin in the lineup.

No free tuner at the start of a recording is a failure with a notice, and the
loop keeps trying for the length of the broadcast. A late start is a short
recording, which is better than none.

## Converting

One ffmpeg process for each recording, fed the broadcast file as it grows.

| Setting | Values | Default |
| --- | --- | --- |
| `recorder_encoder` | `auto`, `software`, `vaapi` | `auto` |

`auto` tries the Intel or AMD hardware encoder through `/dev/dri` with a
one-second test at start, and falls back to software. Hardware needs
`--device /dev/dri` on the container, which `docs/INSTALL.md` will say.

Video is deinterlaced, H.264, with a keyframe every four seconds so a
segment is four seconds. Software encoding is capped at 8 Mb/s; hardware
encoding is constant quality, about 5 Mb/s for 1080 sport. Audio is AAC stereo. **Builder's call:**
stereo first because it plays everywhere; surround in the viewing copy is a
later change, and the broadcast file Plex receives always keeps the original
5.1.

## Where the files live

Two folders, both mounts on the container:

| Mount | What | Notes |
| --- | --- | --- |
| `/recordings` | Broadcast files in flight, and every viewing copy | Local disk. About 4 GB an hour while held |
| `/library` | The Plex library folder | The same folder Plex records into |

Settings name the folder inside `/library` for shows and for films, and the
Plex library each one belongs to, so the scan can be asked for.

With neither mount present the feature is off and the "Recorded by" choice does
not appear. An install that has not opted in is not asked about any of this.

### The name Plex is given

- An episode with a season and number: `Show (Year)/Season 02/Show (Year) - S02E05 - Title.ts`
- Anything dated, which is all sport: `Show (Year)/Season 2026/Show (Year) - 2026-11-15 - Title.ts`
- A film: `Title (Year)/Title (Year).ts`

The guide sync starts keeping the season and episode numbers Plex already
sends. Characters a filesystem refuses are replaced, never dropped silently into
an empty name.

## The client API

Read by the Apple TV app, and by the web page. JSON, under `/api/tv`.

| Call | What it answers |
| --- | --- |
| `GET /api/tv/home` | Three lists: recording now, recent recordings, on now |
| `GET /api/tv/recordings/{id}` | One recording: state, times, position, playlist address |
| `POST /api/tv/recordings/{id}/position` | The player's position. This is also the "somebody is watching" signal |
| `POST /api/tv/live/{vcn}` | Start or join live viewing of a channel. Answers a playlist address |
| `POST /api/tv/live/{vcn}/keepalive` | Still watching. A session nobody keeps alive is closed after a minute |
| `GET /hls/{id}/index.m3u8` and its segments | The viewing copy |

With sign-in off, which is the default, these are open on the network like the
rest of the app. With sign-in on, the app sends a device key that an
administrator makes in Settings. The key is stored hashed, like a session.

## The Apple TV app

SwiftUI, tvOS 17 and later, in `clients/apple/` (it began as
`clients/appletv/`, and moved when the iPhone app joined it). It uses the system player, so
the remote, scrubbing and the info panel behave like every other app on the
box.

It wears the same clothes as the web app: the night-blue ground and the amber
from the logo, the elephant mark, the wordmark. Amber means CouchElephant and
blue-grey means Plex, here as everywhere.

### First screen

**Live now.** Recordings in progress. Each card shows the programme, the
channel, and how much has been recorded. Selecting one offers "Start from
beginning" and "Join live". A recording Plex is making shows a lock and "Cannot
play while Plex is recording".

**Recent recordings.** Newest first, with the "Moving to Plex" and "In Plex"
badges. A recording stopped part way shows a resume bar.

**On now.** Each channel with its current programme. Selecting one watches it
live.

### In the player

Skip back and forward anywhere from the start to live. A "Live" button jumps to
the live point.

### Finding the server

The first run asks for the server address once, typed. **Builder's call:**
no search of the network. CouchElephant runs in a container that cannot
announce itself by Bonjour without host networking, and an address typed once
is less to go wrong than a search that finds nothing.

### Not in the first version

- No guide grid and no schedule changes on the Apple TV. The web app keeps those.
- One shared resume position.
- No iPhone or iPad build, though nothing in the app prevents one.

## What is deliberately not built

- **Commercial detection.** Plex does it after the handover, on its own copy.
- **A second tuner type.** The tuner code is one small module so another can be
  added, and none is added now.
- **Recording without Plex.** The guide still comes from Plex. If Plex is down,
  the last sync still holds and scheduled recordings still run, but nothing new
  can be found.
- **Deleting from Plex.** CouchElephant removes what it holds. A recording that
  has gone to Plex is Plex's to delete.

## Found during the build

- **Apple's player ignores a seek made before the stream has loaded**, and
  opens a growing playlist at its live end. Measured in the tvOS simulator:
  asked for zero, the first frame it took was at 109 seconds. The playlist
  now says it itself: `index.m3u8?start=0` carries `#EXT-X-START` at offset
  zero, and after that the first frame was at second 1.
- **A paired television also needs the channel logos**, so its key opens
  `/logo/` as well as `/api/tv/` and `/hls/`.
- **The web app got the same three lists**, as a Live TV tab, with hls.js for
  browsers that do not play HLS themselves.
- **The show folder is the one the library already has.** The guide does not
  say a show's year, so `Show (Year)` cannot be built from it. A folder named
  for the show, with or without a year, is reused; a new show gets the plain
  name.
- The season and episode numbers were added to the guide sync as planned.
- **The hardware encoder is asked for constant quality, not a bitrate.** The
  free Intel driver Debian ships has no other mode. The first live deploy
  passed the one-second probe and failed every real conversion, because the
  probe asked for less than the conversion did. Both now run the same
  settings, and a hardware failure mid-recording is logged and handed to the
  software encoder. Measured on an i5-1135G7: 1080i in, 1080p60 out at about
  5 Mb/s, in real time, on less than one core.

## Build order

1. The tuner module and the recorder: capture, convert, the state machine, the
   loop. Proven against the real tuner.
2. Booking: the "Recorded by" choice on a recording and on a pass.
3. The handover to Plex.
4. The client API, and live viewing.
5. The web pages: the choice, the badges, the list of what is live.
6. The Apple TV app.
7. Documentation: INSTALL, RECORDING, ARCHITECTURE, API, the changelog.
