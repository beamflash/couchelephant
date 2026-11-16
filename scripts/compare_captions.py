"""One broadcast file through the recorder's encoder, captions side by side.
In the container: PYTHONPATH=/app python /tmp/compare_captions.py <file.ts> <encoder>"""
import os
import re
import shutil
import subprocess
import sys

from app import recorder

src, enc = sys.argv[1], sys.argv[2]
d = os.path.dirname(src)
work = f"{d}/view"
shutil.rmtree(work, ignore_errors=True)
os.makedirs(work)
laced = recorder.interlaced(src)
with open(src, "rb") as f:
    subprocess.run(recorder.convert_args(enc, work, laced), stdin=f, capture_output=True)
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", f"{work}/index.m3u8", "-c", "copy", f"{d}/out.ts"])


def cc(path):
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
                    "-map", "0:1", path + ".srt"])
    text = open(path + ".srt", encoding="utf-8", errors="replace").read()
    return [re.sub(r"<[^>]*>|\{[^}]*\}", "", ln) for ln in text.splitlines()
            if ln and not ln[0].isdigit()]


print("laced", laced)
a, b = cc(src), cc(f"{d}/out.ts")
for i in range(max(len(a), len(b))):
    print(f"{(a[i] if i < len(a) else ''):<45} | {b[i] if i < len(b) else ''}")
