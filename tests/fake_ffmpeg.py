#!/usr/bin/env python3
"""Stands in for ffmpeg, so the suite needs no codecs and takes no time.

It keeps the one promise the recorder relies on: read the broadcast from
stdin as it arrives, grow an event playlist while it does, and close the
playlist when stdin closes.
"""
import os
import sys

args = sys.argv[1:]
if "lavfi" in args:                 # the hardware encoder probe
    sys.exit(1)
playlist = args[-1]
out = os.path.dirname(playlist)
if os.environ.get("FAKE_FFMPEG_FAIL"):
    sys.exit(3)

with open(playlist, "w") as pl:
    pl.write("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:4\n"
             "#EXT-X-PLAYLIST-TYPE:EVENT\n")
    pl.flush()
    n = 0
    while True:
        chunk = sys.stdin.buffer.read1(64 * 1024)
        if not chunk:
            break
        name = f"seg{n:05d}.ts"
        with open(os.path.join(out, name), "wb") as seg:
            seg.write(chunk)
        pl.write(f"#EXTINF:4.000000,\n{name}\n")
        pl.flush()
        n += 1
    pl.write("#EXT-X-ENDLIST\n")
