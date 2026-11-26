#!/usr/bin/env python3
"""A stand-in ffmpeg for the on-demand segments: reads -ss and -start_number,
logs how it was started, and writes one small segment every FAKE_FFMPEG_DELAY
seconds from that number on, as the real one does with temp_file."""
import os
import sys
import time

a = sys.argv
start = int(a[a.index("-start_number") + 1])
pattern = a[a.index("-hls_segment_filename") + 1]
log = os.environ["FAKE_FFMPEG_LOG"]
with open(log, "a") as f:
    ss = a[a.index("-ss") + 1] if "-ss" in a else "0"
    off = a[a.index("-output_ts_offset") + 1] if "-output_ts_offset" in a else "-"
    f.write(f"{os.getpid()} ss={ss} offset={off} start={start}\n")
delay = float(os.environ.get("FAKE_FFMPEG_DELAY", "0.05"))
n = start
while True:
    time.sleep(delay)
    path = pattern % n
    with open(path + ".tmp", "wb") as f:
        f.write(b"ts%d" % n)
    os.replace(path + ".tmp", path)
    n += 1
