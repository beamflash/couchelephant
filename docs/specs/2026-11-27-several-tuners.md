# Several HDHomeRun boxes at once

Status: design, 2026-11-27. Part 2 of `2026-11-27-january-launch.md`. Plan to
follow in `docs/plans/`.

## Why

CouchElephant's recorder knows one HDHomeRun. `app/tuner.py` keeps a single
address: the `tuner_url` setting, or `tuner_detected`, the first box in Plex's
DVR device list that answers `/discover.json`. People add a second box to get
more tuners (a playoff Sunday wants three or four games at once), or to reach
channels the first box cannot (a second antenna, cable, or an IPTV emulator
such as Threadfin). Multiview (part 5) needs one tuner per tile, so it needs
every tuner the house has.

Success: a house with two boxes records four games at once, watches a fifth
channel live, and the Settings page and the apps show both boxes and what each
tuner is doing.

## Goals

- **Find every box and keep them all.** Every HDHomeRun Plex lists (Modes 1
  and 2), every box that answers on the home network, and any address the
  user types.
- **Count free tuners across all boxes**, and pick a box for each recording
  and each live view.
- **Only a box that carries the channel.** Each box's own `/lineup.json`
  decides. Two boxes may carry different channels.
- **Keep the rules that exist:**
  - Never take a tuner Plex holds. The box itself enforces this: `/auto/v<n>`
    refuses when all its tuners are busy, and CouchElephant never asks for a
    named tuner.
  - A recording outranks CouchElephant's own live viewing (`_make_room`).
  - Watching a channel that is recording reuses that stream (`start_live`
    joins `recording_on`).
- **Show each box and its tuners** in Settings (web) and in the apps' Settings.
- **Gate-ready.** One function answers "how many boxes may this install use".
  Today it answers "all". The unlock decides later.

## Non-goals

- Tuners that are not HDHomeRun or an HDHomeRun emulator.
- A guide for channels Plex does not list. In Modes 1 and 2 the guide stays
  the first Plex DVR's guide, so a second box adds tuners for those channels.
  Guides from several Plex DVRs and from Schedules Direct or XMLTV belong to
  Mode 3 (part 3), which builds on this.
- Choosing a named tuner (`/tuner2/v5.1`). `/auto/` stays, because it is what
  keeps Plex's tuners safe.

## Anti-goals (a failure even if it works)

- A recording fails because one box is off or slow, while another box that
  carries the channel is free.
- CouchElephant takes a tuner Plex is using.
- An existing one-box install changes behaviour or needs a new setting after
  the upgrade.
- A typed address is lost or overwritten by discovery.

## Design

### The box list

A new table `tuner_boxes`, one row per box:

| column | meaning |
|---|---|
| `device_id` | `DeviceID` from `/discover.json`; the key, so a box that changes address is the same box |
| `address` | `http://host[:port]`, as last reached |
| `name`, `model`, `firmware` | from `/discover.json` (`FriendlyName`, `ModelNumber`, `FirmwareVersion`) |
| `tuner_count` | `TunerCount` |
| `found_by` | `typed`, `plex` or `network` |
| `enabled` | the user can turn a box off without removing it |
| `last_seen`, `error` | the last answer, and the last failure in words |

- The old `tuner_url` and `tuner_detected` settings migrate into rows on the
  first start (`typed` and `plex`). An install with one box behaves exactly as
  before.
- A `typed` row is never removed or changed by discovery, only by the user.
- Discovery runs at start and then every 10 minutes, off the request path:
  Plex's device list (Modes 1 and 2), then the HDHomeRun network broadcast
  (UDP 65001, the same one the HDHomeRun app uses). In a Docker container on
  a bridge network the broadcast does not reach the LAN; that is reported in
  Settings ("Boxes on your network cannot be found from inside this container.
  Add them by address.") and nothing breaks.

### Which box carries which channel

- Each enabled box's lineup (`/lineup.json`) is read and kept for 10 minutes,
  as `tv.py` already does for refused channels. A box that does not answer
  keeps its last lineup for up to an hour, marked stale.
- A channel number means the same channel on every box in Modes 1 and 2 (one
  Plex guide). Mode 3 adds a source to the key; this design keeps the box
  choice behind one function so that change stays in one place.
- `refusal()` (DRM, ATSC 3.0 AC-4) is per box: one box may carry the ATSC 1.0
  station and another only the 3.0 one.

### Picking a box

`tuner.pick(vcn)` returns an ordered list of (box, stream URL):

1. Only enabled boxes whose lineup carries the channel and do not refuse it.
2. Most free tuners first (`/status.json`).
3. A box whose `/status.json` does not answer (some emulators) counts as
   "unknown". Its free count is `tuner_count` minus the streams CouchElephant
   itself holds on it, and it sorts after boxes that answered.
4. Ties go to the box with the fewest of our own streams, then the typed
   order.

The recorder tries the list in order. A refusal (HTTP 503, "All tuners in
use") or a connection error moves to the next box at once. Only when every
box refuses does the recording carry the existing error and retry on the next
tick. The box used is stored on the recording (`tuner_box`), for the Settings
view and for the "free" count.

### The rules that exist, with several boxes

- **Free tuners** (`tuner.free()`) becomes the sum over enabled boxes.
  `free(vcn)` is the sum over boxes that carry that channel; `_make_room` and
  the overtime busy check (`overtime._tuner_wanted`) use `free(vcn)` for the
  channel that is waiting, because a free tuner on a box without that channel
  does not help.
- **A recording outranks live viewing:** `_make_room` ends one of our own live
  sessions, and prefers one on a box that carries the waiting channel.
- **Reuse:** unchanged. A live view of a channel that is recording joins that
  recording, whatever box it is on.
- **Channels per game** (`passes._tuner_total`) uses the total over all boxes.

### Settings and the apps

- Web Settings, Recording: a list of boxes. Each shows its name, model,
  address, how it was found, and one line per tuner: free, or what holds it
  ("Recording: Chiefs at Bills", "Live: 5.1", "In use by another app", which
  includes Plex). Buttons: Add by address, Turn off, Remove (typed only).
- `GET /api/tv/tuners` returns the same, for the apps. Apple Settings shows
  the list read-only, plus "Add by address".
- The single "Tuner address" field goes away; its value is the first typed
  row.

### Gating

`tuner.allowed_boxes()` is the only place that limits the count. Every other
function reads boxes through it. It returns every enabled box today.

## Settled after the builder check (my calls, 2026-11-27)

1. **A box that fails during a recording.** Failover lives in `Capture`. It
   takes a function that returns the ordered (box, URL) list, not one URL. On
   a 503, a refused connection, or no data for 15 seconds, it asks for the
   list again and moves to the next box. It keeps appending to the same
   `broadcast.ts`, as it already does after a dropped connection today, so the
   converter and the file stay as they are. The gap is recorded on the row
   (`gaps`, seconds lost) and shown on the recording ("12 seconds missing at
   1:04:10, the tuner stopped answering"). `_start_due` no longer calls
   `stream_url`; it hands `Capture` the picker.
2. **Ours or someone else's.** CouchElephant knows its own streams: each
   running `Capture` records its box and channel in memory. A busy tuner on
   `/status.json` is ours when the box and channel match one of ours and the
   count of ours on that channel is not used up; every other busy tuner is
   "In use by another app". `TargetIP` is not used, because Plex and
   CouchElephant often run on one host.
3. **Rows without a good `DeviceID`.** A row is keyed by its own id. The
   `DeviceID` is stored when known and used only to merge: two rows with the
   same non-empty `DeviceID` and the same `TunerCount` are one box. A typed
   address offline at migration gets a row with no `DeviceID` until it
   answers. When a typed row and a found row are the same box, the typed row
   keeps its address and the found row is dropped. An emulator that reports a
   `DeviceID` already used by a box with a different address or tuner count
   is kept as its own row (no merge).
4. **The tuner count.** `TunerCount` from `/discover.json`; `len(status.json)`
   only when `TunerCount` is missing. `total()` counts enabled boxes that
   answered within the last hour, so channels per game does not count a box
   that is off.
5. **`free(vcn)` and the waiting recordings.** `_make_room(now, vcn)` takes the
   channel that waits. It ends one of our live sessions only on a box that
   carries that channel; if none qualifies, it does nothing, and the recording
   waits as it does today. It runs once per waiting recording, oldest first.
   `overtime._tuner_wanted` asks `free(vcn)` for each waiting recording's
   channel and returns true when any of them has no free tuner.
6. **A stale lineup.** A box with a stale lineup is still listed by `pick`,
   last, so a flaky box is tried after every healthy one.
7. **One-box installs.** When no box has a usable lineup, `pick` returns every
   enabled box with the hand-built `/auto/v<n>` URL, as `stream_url` does
   today. One box with no lineup therefore records exactly as before.
8. **The apps' API.** `GET /api/tv/tuners` returns
   `{"ok", "boxes": [{"id", "name", "model", "address", "found_by",
   "enabled", "error", "stale", "tuners": [{"state": "free"|"ours"|"other",
   "label"}]}], "free", "total", "can_find_on_network"}`. The web page reads
   the same route. `POST /api/tv/tuners` (form field `address`) adds a box;
   `POST /api/tv/tuners/{id}` (form `enabled`) turns one on or off;
   `DELETE /api/tv/tuners/{id}` removes a typed one. All are admin-only,
   like the other settings routes.

## Testing

- Unit tests with two fake boxes (the existing fake HDHomeRun, started twice
  with different lineups and tuner counts): pick order, fallback on 503, a
  dead box, a box with no `/status.json`, `free(vcn)`, `_make_room` choice,
  migration of the old settings, a typed row surviving discovery, a box
  that stops answering mid-recording (the recording moves to the other box
  and the gap is recorded), and a one-box install with no lineup.
- Real test, owner's network: the Flex 4K plus a Threadfin container as the
  second box. Threadfin's playlist points at short test streams served on the
  LAN (an ffmpeg test pattern), never at a real channel source, and uses one
  channel number the Flex also has and one it does not. This test also
  answers part 3's first question: does the recorder work through an emulator,
  and what is lost without `/status.json`.
- Apple UI test: the tuner list in Settings against the test server with two
  fake boxes.

## Left to the builder

- Exact Settings layout and wording, within the house style.
- How long a stale lineup is trusted, within the limits above.
- Whether discovery uses `hdhomerun_config` or a small UDP sender (no new
  dependency is preferred).
