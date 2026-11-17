# Plan: ad skip, sports overtime

Date: 2026-11-16. Status: steps 1 and 2 built in 1.5.14 (2026-11-17): the resume fix, the TV and Movies words, and ad skip. Step 3, sports overtime, built in 1.5.15 (2026-11-17), see `app/overtime.py`.

## Why

Plex DVR users complain about the same things for years, and Plex has not fixed them:

- Watching a recording while it records is broken, and you cannot fast-forward through ads in it.
- Sports passes and recordings are poor: wrong airing (already fixed by CouchElephant), and games cut off when they run long.
- Channels DVR fixes most of this, but its recordings in Plex lose ad skip and watch state.

CouchElephant (CE) already plays recordings from the start while they record, fixes the team pass airing choice, and hands recordings into Plex with a resume point. Two gaps remain:

1. No ad skip in CE's own players, during or after recording.
2. No automatic extension when a game runs long.

Guiding rule for all three: CE does the work only where it owns the whole path. Where Plex owns a piece, CE hands over plain data and stops. No database hacks, no stitching files, no automatic actions that lose content when a guess is wrong.

## Facts tested live on mightymouse, 2026-11-16

These are measured, not assumed.

### Plex ad markers

- Every Plex library has the pref `enableAdMarkerGeneration`, label "Ad detection": `0` Disabled, `1` For recorded items (default), `2` For all items. Read with `GET /library/sections/{id}/prefs`.
- His Movies (section 1) and TV Shows (section 2) are both at `1`.
- A CE handover is NOT a "recorded item" to Plex. The butler log said "generated ad markers for 0 show(s)" with two handed-over files present, and both files have no markers.
- The marker job is butler task `ButlerTaskGenerateAdMarkers` (server pref `GenerateAdMarkerBehavior = scheduled`). It runs Plex's bundled comskip: `"/usr/lib/plexmediaserver/Plex Commercial Skipper" --ini=/usr/lib/plexmediaserver/Resources/comskip.ini`.
- Test: a copy of a handed-over Becker episode (`.ts`, 19.5 min) in a temporary library set to `2`. Plex wrote two `type="commercial"` markers (116950 to 237170 ms, 720650 to 961060 ms) in 11 seconds.
- The same run also marked a Plex DVR recording (a full NFL game, 9 minutes of work), even though the DVR pref `comskipMethod` is `0`. So the library pref, not the DVR pref, drives markers.
- Markers show up in `GET /library/metadata/{key}?includeMarkers=1` as `<Marker type="commercial" startTimeOffset=... endTimeOffset=...>`.
- The own-recorder spec (`docs/specs/2026-11-15-own-recorder.md`, "What is deliberately not built") says "Plex does it after the handover". That is only true when the user sets the library to "For all items". Fix that line to say so.

### Existing drift bug

- CE keeps updating `recordings.position` after the handover (CE still plays a recording that is in Plex), and never re-sends it. Becker episode: Plex resume 4:13, CE position 5:00.
- Fix: when the position of an `in_plex` recording changes, CE calls `Plex.set_position` (`app/plex.py:374`) again with the owner token it already uses. CE has one resume point per recording; per-person resume is not planned (CE has no multi-user support).

### Operational warning

- Deleting a Plex library made Plex rebuild its full text search tables and optimize the 600 MB `library.db`. One thread held every DB connection for about 6 minutes (20:01 to 20:07). Browsing and `/:/timeline` calls timed out while two remote streams played. It ended by itself.
- Never create or delete a Plex library on mightymouse as a test while anyone is streaming. Do not use a temporary library in automated tests against his server.

## Research summary (outside sources)

- **Comskip live mode**: `live_tv=1` in comskip.ini keeps scanning a growing file until it stops changing; pair with `live_tv_retries`. `standoff_size > 0` is required, or defaults are used.
- **Channels DVR** has "Start live detection when watching in-progress recording". Users and its developer call it alpha quality. Their main bug was not detection: the comskip timestamps were right, but the client skipped at the wrong places, because detection runs on the server and the live buffer runs on the client. CE avoids this, because one ffmpeg run writes both the `.ts` and the HLS output on one timeline.
- **OTA broadcasts have no SCTE-35 ad cues.** Detection from picture and sound (black frames, silence, logo) is the only option.
- **Plex has no API to write commercial markers.** Writes return 400. Editing `library.db` needs Plex stopped and is unsupported. Do not do it.
- **Cable boxes extend sports with Gracenote's paid live sports feed** ("DVR Extend"). Not available to us.
- **ESPN scoreboard API** (`https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard`) is unofficial and undocumented, but long stable. Read `events[].status.type.state` (`pre`, `in`, `post`) and `completed`. Covers NFL, NBA, MLB, NHL, college football and basketball, MLS. MLB (statsapi.mlb.com) and NHL (api-web.nhle.com) have free official APIs.

Sources: Plex forum threads 278653, 485446, 902767, 630122, 868317, 931778, 936928; Channels community threads 28276, 29089, 20265; zuplo ESPN hidden API guide.

## Feature 1: Ad skip

### Design

A. **After recording (solid).** When a CE recording finishes, run comskip on the `.ts`. Store breaks as rows `(recording_id, start_s, end_s, final)`.

B. **While recording (with rules).** When a recording starts, also start comskip with `live_tv=1` on the growing `.ts`. Read its output as it grows. Two rules keep it clean:
   - Publish a break only after it has ended at least 60 seconds behind the live edge. Comskip revises breaks near the edge; never show those.
   - Never skip on its own. Show a Skip button while the play head is inside a published break. A wrong break costs one tap, not missed footage.
   - When the recording ends, run pass A and replace the live rows with the final ones.

C. **Players.**
   - tvOS: `AVPlayerViewController.contextualActions` (the native "Skip Intro" spot), set while inside a break, cleared after.
   - iOS: the same control in `PlayerView.swift` where the platform allows it, else an overlay button.
   - Web: a button in `app/static/js/player.js`.
   - Times are in recording seconds, the same clock as `?start=0` and `recordings.position`. Check the HLS start offset once (first PTS) so marker times line up with player time.

D. **Plex copy: not CE's job.** After the handover, ad markers are the user's own Plex business. CE writes nothing into Plex for ads and changes no Plex setting. The docs say one thing: Plex's default "For recorded items" does not mark CE handovers, so a user who wants Plex markers on them sets that library's "Ad detection" to "For all items" in Plex.

E. **Settings (decided 2026-11-17).** Two switches, both on by default, in `app/routes/settings.py` `KEYS` so the web page and both apps show them:
   - `adskip_final` "Skip ads in finished recordings": pass A.
   - `adskip_live` "Skip ads while still recording": pass B. Also covers the live TV pause buffer, which is a growing file too. The Skip button shows only when the player is behind live and the end of the break is already recorded and published.
   - Off means no scan runs and no Skip button shows. Turning one off keeps the breaks already found.

### Not doing

- Automatic skip.
- Writing markers into Plex's database.
- Cutting ads out of files (a false positive in sports deletes a real play for good).

### Costs and risks

- Comskip is CPU work on mightymouse while a game records. Measure CPU with 2 recordings at once before release; the default is on (Robert, 2026-11-17).
- Comskip is weaker on sports (scorebug logos come and go). Expect misses; the tap-to-skip rule makes misses cheap.
- Comskip must be in the CE image. Check license (GPL) and add to THIRD_PARTY.md. Plex's bundled binary is not ours to use.

### Code pointers

- Recorder jobs and ffmpeg: `app/recorder.py` (capture `start_capture` around line 415, converter `start_converter` around 420, end of recording around 759 to 768).
- Handover: `app/handover.py` (`hand_over`).
- Player endpoints: `app/routes/tv.py`; `GET /api/tv/recordings/<id>/now` is a model for a new `GET /api/tv/recordings/<id>/breaks`.
- Apple player: `clients/apple/Shared/PlayerView.swift`.

## Feature 2: Sports overtime

### Design

- Only for recordings CE DVR makes. Plex cannot extend a recording that is already running, and stitching a second CE recording onto Plex's would be janky. Sports passes already default to CE DVR when the recorder is on (`auto`).
- The recorder stops a job when `stops_at <= now` (`app/recorder.py:759`). Overtime is: move `stops_at` later while the game is live.
- Match a recording to a scoreboard event once, at booking or at start: league from the guide's sport, both team names, and the date. CE already has team ids and names from the Plex guide (`teams` table). Store the matched event id on the recording.
- From 10 minutes before the scheduled end, poll every 2 minutes:
  - `state == "in"`: set `stops_at = now + 10 min`.
  - `state == "post"` or `completed`: set `stops_at = now + 5 min`, stop polling.
  - Hard cap: never more than `ends_at + overtime_cap` (default 3 hours, a setting).
- **Fallback:** if matching fails or the scoreboard does not answer, keep today's padding (`SPORTS_PADDING`, 1 min before, 30 min after, `app/routes/record.py:73`). Overtime can only make a recording longer, never shorter.
- **Tuners:** an extended job holds its tuner past its booking. Before extending, check the next bookings. If no tuner would be free, the next booking wins and the overtime ends, with a notice saying so. Consider a setting to keep one tuner free during sports.
- Live captions and the "what is on" header (`/now`) should keep naming the game during overtime, not the next guide programme.

### Not doing

- Overtime for Plex-recorded items.
- Guessing the end from the video (no reliable signal in OTA).

### Risks

- The ESPN API is unofficial and can change without notice. Keep the source behind one small module with a health check, and alert on failures (existing `notify` path). Prefer the official MLB and NHL APIs for those leagues.
- Team name matching across sources (guide vs ESPN). Store and test the mapping; log every match.

## Also this round: words for TV and movies (Robert, 2026-11-17)

On every screen (web, Apple TV, iPhone) and in every message the server sends:

- The TV category is "TV", never "Shows". One programme is a "series". The movie category is "Movies", never "Films".
- Code names (`shows`, `recorder_shows_folder`, section types) stay as they are. Only text people read changes.

Found on 2026-11-17 (search again before the change, the list may be short):

- `app/templates/_settings.html`: "Shows section" (267), "Shows and sport" (416), "Films" (426), "Adds films" (316).
- `clients/apple/Shared/SettingsView.swift`: "Shows folder", "Shows library", "Films folder", "Films library" (201 to 204).
- `clients/apple/Shared/NewPassView.swift`: "Show or film" (35), "A show the guide has not reached" (43).
- `app/filters.py`: "Film" (16), and "Series" (18) stays.
- `app/smartfilter.py`: "TV show" (93).
- `app/handover.py`: "films" and "shows" in the missing folder message (51).
- `app/health.py`: "for films" (214).
- `app/routes/tv.py`: "Pick a show." (759).
- `app/notify.py`: "without the show" (99).

## Order of work

1. The drift bug fix and the TV and Movies words (small).
2. Ad skip: A (after recording) and the players first, then B (live).
3. Overtime (most moving parts).

Each step ships on its own with tests, through the normal release path (`docs/RELEASING.md`). Builds 1.5.5 to 1.5.12 are still untagged; settle that first.

## Decisions before code (ask Robert)

1. Confirm the order above.

## Test notes

- Do not test against his live Plex by adding or deleting libraries (see Operational warning).
- Resume writes to Plex can be tested on a real item and put back with `/:/progress` at the old time, as done on 2026-11-16.
- `deploy.sh` refuses while a recording is active; respect that (`COUCHELEPHANT_DEPLOY_ANYWAY=1` only with his yes).
- Check another open session is not editing the same CE files before starting.
