# Surround sound in the viewing copy

Status: approved by the owner 2026-11-28 ("Yes fix"), from the check listed
under "Before reviewer codes go out" in `2026-11-27-january-launch.md`.

## Why

Every viewing copy and every on-demand segment is mixed down to AAC stereo
(`recorder.convert_args`: `-c:a aac -ac 2`). A game broadcast in 5.1 plays in
stereo on an Apple TV connected to a receiver. Channels and Plex keep 5.1, so
reviewers will notice.

## What changes

When the broadcast's first sound track is AC-3 (every ATSC 1.0 station),
each segment carries two sound tracks:

1. The broadcast's own track, copied untouched (5.1 when the station sends
   5.1, 2.0 when it sends 2.0).
2. The AAC stereo track made today.

Any other sound (E-AC-3, AAC, MPEG audio from an IPTV source, no sound) gets
the AAC stereo track alone, exactly as today. E-AC-3 is left out because the
web player's hls.js (1.5.17) stops reading the track list at an E-AC-3 track
and never reaches the stereo track after it: picture, no sound (Opus review).

## Tested before the design was fixed (2026-11-28)

Test file: H.264 with 5.1 AC-3, made into HLS three ways. Results:

| Player | Plays |
|---|---|
| AVPlayer (macOS 26.6, the engine of Apple TV, iPhone and iPad) | AC-3, 6 channels |
| Safari 27.2 with hls.js (the web player) | AC-3 |
| hls.js with AC-3 reported unsupported (how Chrome and Firefox answer) | AAC stereo |
| AVPlayer, E-AC-3 5.1 source | E-AC-3, 6 channels |
| AVPlayer, 2.0 AC-3 that changes to 5.1 mid-stream | plays through the change |

hls.js takes the first sound track whose codec the browser supports, so the
copied track goes first. The alternative, a separate sound playlist named in
the master playlist, made AVPlayer on a stereo output pick the stereo variant,
and it needs a second set of files for every segment. It was dropped.

## Decisions (my calls)

- **How the codec is known.** ffprobe reads the first 64 KB of the broadcast
  (about 0.1 s of a station, 0.12 s to probe) before ffmpeg starts. This adds
  about 0.2 s to a channel change. The hardware path already waits for 1 MB.
  When ffprobe cannot say, the copy is stereo only, as today.
- **On demand.** The answer is cached in `od/audio`, beside `od/v0`, whatever
  it is ("none" when ffprobe cannot say), so a recording never gets two
  layouts. `od/duration`, `od/v0` and `od/audio` are written whole (temporary
  name, then rename). `ENC_VERSION` goes to 3; a segment on disk from an
  older version is never served, and the first new one clears them.
- **Where two on-demand runs meet** (a viewer seeks back and plays on into a
  later run's first segment), the copied sound of the later run starts at its
  seek point (`-copypriorss:a:0 0`). Measured: 32 to 64 ms of silence at that
  join, never sound that repeats. The stereo track, encoded per run, was
  already within one AAC frame there.
- **Viewing copies made before the upgrade** stay stereo. A converter always
  starts again from the first byte, so a copy never mixes the two layouts.
- **Plex** is unchanged: the handover copies `broadcast.ts`, which always had
  the broadcast's own sound.
- **The Apple apps** need no change: the audio session already uses
  `.moviePlayback`, and AVPlayer passes AC-3 to a receiver.
- **A second language track (SAP)** is out of scope, as today.

## Testing

- `convert_args`: two `-map 0:a:0?`, the copy first, stereo AAC second, only
  when asked; one track otherwise.
- Real ffmpeg: a 5.1 AC-3 source gives segments with AC-3 6 channels then
  AAC 2 channels; an MPEG audio source gives AAC 2 channels alone; on-demand
  runs started at different segments give the same sound packet times.
- The converter asks ffprobe and passes the answer; a source ffprobe cannot
  read gives stereo only.
