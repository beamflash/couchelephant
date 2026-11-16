# Broadcast formats

CouchElephant DVR records whatever an HDHomeRun tuner sends, untouched, and
makes a second copy to watch (HLS, H.264 and AAC). This page lists the
formats that copy can meet, how each is handled, and how that was checked.
Most of them are not on the air where the project is developed, so they are
tested with clips made from a real broadcast (see "Checking it again").

## What can arrive

| Where | System | Pictures | Sound | Captions |
|---|---|---|---|---|
| US, Canada, Mexico, South Korea | ATSC 1.0 | MPEG-2 or H.264: 1080i 29.97, 720p 59.94, 480i 29.97 (16:9 and 4:3), sometimes 1080p 23.98/29.97 | AC-3, sometimes E-AC-3 | CEA-608/708 inside the video (ATSC A/53 Part 4) |
| US (NextGen TV) | ATSC 3.0 | HEVC | AC-4 | IMSC1 |
| UK, Europe, Australia, New Zealand | DVB-T/T2/C | MPEG-2 or H.264 (some HEVC): 576i 25, 1080i 25, 720p 50 | MP2, AC-3, E-AC-3, AAC-LATM | DVB bitmap subtitles or teletext |

## How the copy is made

- **Sound.** The first sound track is turned into stereo AAC, whatever it was.
  A subchannel with no sound at all is fine: the sound is optional.
- **Timing.** Every picture keeps the broadcast's own time stamp. A constant
  rate filled gaps in a broadcast with copies of pictures, and each copy
  repeated its captions.
- **Interlacing.** A picture 1080, 576 or 480 lines high takes the interlaced
  path, whatever its first seconds say: a channel can open on film or an
  advert sent progressive and turn interlaced at the next programme. 720p is
  progressive by definition and is not deinterlaced. Other sizes go by the
  stream's field order and its first seconds of pictures.
- **Hardware (VAAPI).** Interlaced channels are deinterlaced at field rate, so
  1080i sport keeps 60 pictures a second. That makes two pictures from every
  broadcast picture, and both carried its captions, which doubled every
  letter: the second of each pair now has its captions removed. Progressive
  channels skip the deinterlacer, which otherwise doubled them as well.
- **Software (x264).** Deinterlaced at frame rate, one picture out for each
  picture in, so captions pass through as they are.
- **Captions.** The playlist a player opens names one caption service, CC1,
  without a language, because CC1 is in whatever language the station uses.

## What is not handled

- **ATSC 3.0** channels are refused when asked for: nothing in the container
  can decode AC-4 sound, and many carry DRM (A3SA) anyway.
- **DVB subtitles and teletext** are not carried into the copy. A DVB channel
  plays normally; its Captions menu entry stays empty.
- **CC3/CC4 and 708 services other than the first** (a second language on some
  stations) are not offered. The data is in the copy, but only CC1 is named.

## How it was checked (2026-11-16)

`scripts/format_matrix.py` makes fourteen clips from one captioned 1080i
broadcast and runs each through the recorder's own encoder command, on the
hardware path and the software path. Each must keep its frame count (twice it
for hardware deinterlacing), keep its sound, and keep its captions (at least
0.9 of the text the same). All twenty-eight passed:

1080i MPEG-2 top and bottom field first, 480i 16:9 and 4:3, 1080i H.264, 720p
H.264, 1080p progressive, no sound, E-AC-3, 576i25 with MP2, 1080i25 H.264
with AAC-LATM, 720p50 with E-AC-3, a 1080i channel opening on six seconds of
progressive pictures, and a broadcast with 3 MB missing in the middle.

`scripts/channel_sweep.py` runs every channel a tuner has through the same
command, one tuner at a time and only while two are free and nothing records
or plays.

## Checking it again

Inside the container (`docker cp` the scripts to `/tmp` first):

```
sh /tmp/grab_captioned_clip.sh <tuner ip> <a 1080i channel>
PYTHONPATH=/app python /tmp/format_matrix.py vaapi
PYTHONPATH=/app python /tmp/format_matrix.py software
PYTHONPATH=/app python /tmp/channel_sweep.py vaapi
```
