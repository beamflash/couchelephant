"""Every channel through the recorder's own encoder command, checked.

Runs inside the CouchElephant container (docker cp it to /tmp): PYTHONPATH=/app python /tmp/channel_sweep.py <encoder> [vcn ...]
One tuner at a time, and only while two are free and nothing of ours records
or plays. Writes one JSON line per channel to /tmp/sweep-<encoder>.jsonl.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import httpx

from app import db, recorder, tuner

ENC = sys.argv[1]
HOST = tuner.stream_url("1.1").split(":5004")[0]
OUT = f"/tmp/sweep-{ENC}.jsonl"
W = "/tmp/sweep-work"


def run(args, timeout=180, stdin=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=stdin)


def free_tuners():
    try:
        st = httpx.get(HOST + "/status.json", timeout=5).json()
        return sum(1 for t in st if "VctNumber" not in t)
    except Exception:
        return 0


def ours_busy():
    return db.one("SELECT COUNT(*) c FROM recordings WHERE state = 'recording'")["c"] > 0


def captions(path):
    srt = path + ".srt"
    run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", f"movie={path}[out0+subcc]",
         "-map", "0:1", srt])
    try:
        text = open(srt, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if ln and not ln[0].isdigit()]
    text = " ".join(re.sub(r"<[^>]*>|\{[^}]*\}", "", ln) for ln in lines)
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def probe(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=codec_name,height,field_order,r_frame_rate,nb_read_frames",
             "-of", "json", path])
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    a = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_name", "-of", "csv=p=0", path]).stdout.split()
    return s, a


def one(vcn):
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W)
    src, out_dir = f"{W}/src.ts", f"{W}/view"
    os.makedirs(out_dir)
    got = run(["timeout", "45", "ffmpeg", "-v", "quiet", "-y", "-t", "25", "-i",
               tuner.stream_url(vcn), "-map", "0", "-c", "copy", "-f", "mpegts", src], timeout=60)
    if not os.path.exists(src) or os.path.getsize(src) < 500_000:
        return {"vcn": vcn, "result": "no stream", "detail": got.stderr[-200:]}
    s_in, a_in = probe(src)
    laced = recorder.interlaced(src)
    args = recorder.convert_args(ENC, out_dir, laced)
    with open(src, "rb") as f:
        conv = run(args, timeout=600, stdin=f)
    out = f"{W}/out.ts"
    run(["ffmpeg", "-v", "quiet", "-y", "-i", f"{out_dir}/index.m3u8", "-c", "copy", out])
    if conv.returncode or not os.path.exists(out):
        return {"vcn": vcn, "result": "encode failed", "laced": laced,
                "detail": (conv.stderr or "")[-300:]}
    s_out, a_out = probe(out)
    fin, fout = int(s_in.get("nb_read_frames") or 0), int(s_out.get("nb_read_frames") or 0)
    want = 2 if (laced and ENC == "vaapi") else 1
    ratio = fout / fin if fin else 0
    cin, cout = captions(src), captions(out)
    sim = difflib.SequenceMatcher(None, cin, cout, autojunk=False).ratio() if cin else None
    problems = []
    if not fin or abs(ratio - want) > 0.05:
        problems.append(f"frames {fin} -> {fout}, wanted x{want}")
    if not a_out:
        problems.append("no audio")
    if cin and (sim or 0) < 0.85:
        problems.append(f"captions differ ({sim:.2f})")
    return {"vcn": vcn, "result": "PROBLEM" if problems else ("ok" if cin else "ok, no captions on air"),
            "problems": problems, "codec": s_in.get("codec_name"), "height": s_in.get("height"),
            "field_order": s_in.get("field_order"), "fps": s_in.get("r_frame_rate"),
            "laced": laced, "frames": [fin, fout], "audio": a_in,
            "caption_chars": [len(cin), len(cout)], "similarity": sim}


lineup = httpx.get(HOST + "/lineup.json", timeout=10).json()
wanted = sys.argv[2:] or [c["GuideNumber"] for c in lineup if c.get("AudioCodec") != "AC4"]
for vcn in wanted:
    waited = 0
    while (free_tuners() < 2 or ours_busy()) and waited < 1800:
        time.sleep(15)
        waited += 15
    if waited >= 1800:
        print("stopping: tuners busy for 30 minutes", flush=True)
        break
    try:
        row = one(vcn)
    except Exception as e:
        row = {"vcn": vcn, "result": "error", "detail": repr(e)[:300]}
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(vcn, row["result"], row.get("problems") or "", flush=True)
shutil.rmtree(W, ignore_errors=True)
print("done", flush=True)
