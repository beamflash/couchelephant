# January 2027 launch: what ships, in what order, and the three modes

Status: agreed with the owner 2026-11-27. Each piece below gets its own spec in
`docs/specs/` before it is built, and the owner approves each one.

## Why

The launch target is the NFL playoffs (Wild Card weekend, about 2027-01-09).
The owner wants everything below working and tested by then. Pricing is a free
Apple app plus a one-time $19.99 unlock. Which features sit behind the unlock
is still open, so every feature is built so it CAN be gated, with one check in
one place, and the gate itself is not decided here.

## The three modes

| Mode | Who records | Guide from | Plex needed |
|---|---|---|---|
| 1. Plex add-on | Plex | Plex | yes, with Plex Pass and a tuner in Plex |
| 2. Full DVR with Plex | CouchElephant (or Plex, per booking) | Plex | yes |
| 3. No Plex | CouchElephant | Schedules Direct or XMLTV | no; handing recordings to a Plex library is an option the user turns on |

Facts the owner confirmed on 2026-11-27, so later specs build on them:

- CouchElephant never streams video from Plex. Live TV always comes straight
  from the tuner through CouchElephant's recorder.
- A recording CouchElephant made keeps `recorded_by = couchelephant` after the
  handover; only its state becomes `in_plex`. It still plays in CouchElephant,
  from the file in the Plex folder.
- A recording Plex made does not play in CouchElephant today. Fixing that is
  piece 4 below.

## The pieces, in build order

1. **Signed installers for the server.** Windows through Azure signing (about
   $10 a month, asked with the exact amount before any charge), macOS through
   Apple Developer ID and notarisation. Started first because Azure checks the
   publisher's identity, which can take days.
2. **Several HDHomeRun boxes** (Feature 1). Spec: `2026-11-27-several-tuners.md`.
3. **Mode 3, no Plex.** A guide source that is not Plex (Schedules Direct, then
   XMLTV), CouchElephant's own tuner discovery, its own library. IPTV
   (Feature 2) lands here: an M3U playlist is a tuner, XMLTV is a guide
   source. The emulator test (Threadfin as an HDHomeRun) runs first, and its
   results go to the owner before the IPTV design is fixed.
4. **Plex recordings that play without a stop.** Modes 1 and 2. Plays Plex DVR
   recordings, finished or in progress, from a CouchElephant copy, and moves
   the player to Plex's final file at the same place, with commercial removal
   mapped by matching sound. Quiet unless something goes wrong.
5. **Multiview** (Feature 3) on Apple TV and iPad.
6. **Windows and Mac client apps**, signed, full screen, used like the Apple TV
   app.

If time runs short, pieces 5 and 6 move after the launch. Testing is not cut.

## Later

Jellyfin integration (in `docs/PENDING.md`).

## Before reviewer codes go out

- Check that the viewing copy keeps 5.1 sound.
- Time a channel change against Channels.
