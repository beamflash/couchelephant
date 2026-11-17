#!/usr/bin/env python3
"""Stands in for comskip. Told what to find by a plan file next to the
recordings folder, it writes comskip's two outputs: the .edl of breaks, and
progress lines with how far into the file it has got.

A live run (live_tv=1 in its ini) keeps going until the plan's `stop` file
exists, and then writes `final` breaks, as comskip does at the end of a
recording."""
import json
import os
import sys
import time

ini = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--ini="))
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--output="))
src = sys.argv[-1]
plan = json.load(open(os.environ["FAKE_COMSKIP_PLAN"]))
live = "live_tv=1" in open(ini).read()
edl = os.path.join(out, os.path.splitext(os.path.basename(src))[0] + ".edl")


def write(breaks):
    with open(edl + ".tmp", "w") as f:
        for s, e in breaks:
            f.write(f"{s:.2f}\t{e:.2f}\t0\n")
    os.replace(edl + ".tmp", edl)


if plan.get("fail"):
    sys.exit(1)
if live:
    reached = plan.get("reached", 0)
    print(f" {reached // 3600}:{reached // 60 % 60:02d}:{reached % 60:02d} - 100 frames in 1 sec", flush=True)
    write(plan.get("live", []))
    while not os.path.exists(plan["stop"]):
        time.sleep(0.05)
write(plan.get("final", []))
print("Commercials were found.", flush=True)
