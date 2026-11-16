"""Broadcast formats this tuner never receives, made from a real captioned
1080i clip, each run through the recorder's own encoder command and checked.

Runs inside the CouchElephant container:
    PYTHONPATH=/app python /tmp/format_matrix.py <encoder> [name prefix ...]
Needs /tmp/synth/src.ts: 25 s of a 1080i channel while it carries captions,
which grab_captioned_clip.sh <tuner ip> <channel> makes.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import sys

from app import recorder

ENC = sys.argv[1]
D = "/tmp/synth"
SRC = f"{D}/src.ts"


def run(args, timeout=600, stdin=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=stdin)


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


def frames(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames,height,field_order,r_frame_rate,codec_name",
             "-of", "json", path])
    return (json.loads(r.stdout or "{}").get("streams") or [{}])[0]


def has_audio(path):
    return bool(run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                     "stream=codec_name", "-of", "csv=p=0", path]).stdout.strip())


A = ["-c:a", "ac3", "-b:a", "192k"]
MPEG2_I = ["-c:v", "mpeg2video", "-b:v", "12M", "-flags", "+ilme+ildct", "-a53cc", "1"]
X264_I = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-x264-params",
          "interlaced=1:tff=1", "-a53cc", "1"]
VARIANTS = {
    # name: (ffmpeg output options, captions expected, frame-rate class note)
    "1080i MPEG-2 top field first (US/CA HD, as received here)": (["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"], True),
    "1080i MPEG-2 bottom field first": (["-map", "0:v:0", "-map", "0:a:0", *MPEG2_I, "-top", "0", *A], True),
    "480i MPEG-2 (US/CA SD subchannel)": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "scale=720:480:interl=1", *MPEG2_I, "-top", "1", *A], True),
    "480i MPEG-2 4:3 640x480": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "scale=640:480:interl=1", *MPEG2_I, "-top", "1", "-aspect", "4:3", *A], True),
    "1080i H.264 interlaced (US/CA H.264 subchannel)": (["-map", "0:v:0", "-map", "0:a:0", *X264_I, *A], True),
    "720p59.94 H.264": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "yadif=1,scale=1280:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-a53cc", "1", *A], True),
    "1080p29.97 MPEG-2 progressive (film sent as progressive)": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "yadif=0", "-c:v", "mpeg2video", "-b:v", "12M", "-a53cc", "1", *A], True),
    "1080i MPEG-2 with no sound (data or slideshow subchannel)": (["-map", "0:v:0", "-c", "copy"], True),
    "1080i MPEG-2, E-AC-3 sound": (["-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "eac3", "-b:a", "192k"], True),
    "576i25 MPEG-2, MP2 sound (DVB SD, Europe/Australia)": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "fps=25,scale=720:576:interl=1", *MPEG2_I, "-top", "1", "-c:a", "mp2", "-b:a", "192k"], False),
    "1080i25 H.264, AAC-LATM sound (DVB-T2 HD)": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "fps=25", *X264_I, "-c:a", "aac", "-b:a", "128k", "-mpegts_flags", "latm"], False),
    "720p50 H.264, E-AC-3 sound (DVB HD)": (["-map", "0:v:0", "-map", "0:a:0", "-vf", "yadif=1,fps=50,scale=1280:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "eac3", "-b:a", "192k"], False),
    "1080i that opens on 6 s of progressive pictures (film or advert first)": (None, True),
    "Signal dropout: 3 MB of broadcast missing mid-stream": (None, True),
}


def special(name, out):
    """Clips made from pieces rather than one ffmpeg command."""
    if name.startswith("1080i that opens"):
        prog = f"{D}/prog.ts"
        run(["ffmpeg", "-v", "error", "-y", "-t", "6", "-i", SRC, "-map", "0:v:0", "-map", "0:a:0",
             "-vf", "yadif=0", "-c:v", "mpeg2video", "-b:v", "12M", "-a53cc", "1", *A, prog])
        if not os.path.exists(prog):
            return False
        # A transport stream joins by bytes, the way a channel switches content.
        with open(out, "wb") as o:
            for part in (prog, SRC):
                o.write(open(part, "rb").read())
        return True
    if name.startswith("Signal dropout"):
        data = open(SRC, "rb").read()
        third = len(data) // 3
        open(out, "wb").write(data[:third] + data[third + 3_000_000:])
        return True
    return False


def make(name, opts):
    out = f"{D}/{re.sub(r'[^a-z0-9]+', '-', name.lower())[:40]}.ts"
    if opts is None:
        return out if special(name, out) else None
    r = run(["ffmpeg", "-v", "error", "-y", "-i", SRC, *opts, "-f", "mpegts", out])
    return out if r.returncode == 0 and os.path.exists(out) else None


def check(name, path, want_cc):
    work = f"{D}/view"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    laced = recorder.interlaced(path)
    with open(path, "rb") as f:
        conv = run(recorder.convert_args(ENC, work, laced), stdin=f)
    out = f"{D}/out.ts"
    run(["ffmpeg", "-v", "quiet", "-y", "-i", f"{work}/index.m3u8", "-c", "copy", out])
    if conv.returncode or not os.path.exists(out):
        return {"format": name, "result": "ENCODE FAILED", "detail": conv.stderr[-300:]}
    si, so = frames(path), frames(out)
    fin, fout = int(si.get("nb_read_frames") or 0), int(so.get("nb_read_frames") or 0)
    want = 2 if (laced and ENC == "vaapi") else 1
    cin, cout = captions(path), captions(out)
    sim = (difflib.SequenceMatcher(None, cin, cout, autojunk=False).ratio() if cin else None)
    problems = []
    if not fin or abs(fout / fin - want) > 0.03:
        problems.append(f"frames {fin} -> {fout}, wanted x{want}")
    if has_audio(path) and not has_audio(out):
        problems.append("sound lost")
    if want_cc and not cin:
        problems.append("test clip lost its captions while being made")
    if cin and sim < 0.9:
        problems.append(f"captions differ ({sim:.2f})")
    return {"format": name, "result": "PROBLEM" if problems else "ok", "problems": problems,
            "laced": laced, "height": si.get("height"), "fps_in": si.get("r_frame_rate"),
            "frames": [fin, fout], "caption_chars": [len(cin), len(cout)],
            "similarity": None if sim is None else round(sim, 3)}


only = sys.argv[2:]
for name, (opts, want_cc) in VARIANTS.items():
    if only and not any(name.startswith(o) for o in only):
        continue
    path = make(name, opts)
    row = check(name, path, want_cc) if path else {"format": name, "result": "could not make test clip"}
    print(json.dumps(row), flush=True)
