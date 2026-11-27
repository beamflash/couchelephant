"""An HDHomeRun that answers like the real one.

Served over real HTTP on localhost, so `tuner.py` and the capture are run
through httpx and not mocked away. Reproduced from the real box:
  - `/lineup.json` lists each channel with the URL it streams from
  - `/status.json` has one row per tuner, and a busy one carries `VctNumber`
  - a stream is an endless body, and ends only when the reader hangs up
  - with every tuner busy, the stream answers 503
"""
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = b"\x47" + b"\x00" * 187          # one MPEG-TS packet, more or less


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.total = 4
        self.plex_holds = []        # channel numbers Plex has a tuner on
        self.streams = []           # every channel number ever streamed, in order
        self.live = []              # channel numbers streaming right now
        self.silent = False         # connect, then send nothing
        self.extra = []             # lineup rows a test adds: DRM, ATSC 3.0


STATE = State()
_URL = ""

LINEUP = [("5.1", "KCTV"), ("9.1", "KMBC"), ("41.1", "KSHB")]

# Real pictures, for the Apple apps' clicking tests: each stream is a test
# pattern made by ffmpeg as it is sent, a different colour per channel, at
# the speed of a broadcast. The unit tests keep the cheap empty packets.
VIDEO = os.environ.get("FAKE_TUNER_VIDEO") == "1"
# How big a picture it is. The defaults are a broadcast's own; the public demo
# turns them down (FAKE_TUNER_SIZE=640x360, FAKE_TUNER_RATE=24,
# FAKE_TUNER_BITRATE=1M) so a small server can make a few at once.
SIZE = os.environ.get("FAKE_TUNER_SIZE", "1280x720")
RATE = os.environ.get("FAKE_TUNER_RATE", "30000/1001")
BITRATE = os.environ.get("FAKE_TUNER_BITRATE", "4M")


def _pattern(vcn):
    hue = (sum(map(ord, vcn)) * 47) % 360
    return subprocess.Popen(
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", f"testsrc2=size={SIZE}:rate={RATE}",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-vf", f"hue=h={hue}", "-c:v", "mpeg2video", "-b:v", BITRATE, "-g", "15",
         "-c:a", "ac3", "-b:a", "192k", "-f", "mpegts", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/lineup.json":
            rows = [{"GuideNumber": n, "GuideName": name, "VideoCodec": "MPEG2",
                     "AudioCodec": "AC3", "URL": f"{_URL}/auto/v{n}"}
                    for n, name in LINEUP]
            return self._json(rows + STATE.extra)
        if self.path == "/status.json":
            busy = list(STATE.plex_holds) + list(STATE.live)
            rows = [{"Resource": f"tuner{i}", "VctNumber": v}
                    for i, v in enumerate(busy)]
            rows += [{"Resource": f"tuner{i}"} for i in range(len(busy), STATE.total)]
            return self._json(rows[:max(STATE.total, len(busy))])
        if self.path.startswith("/auto/v"):
            if len(STATE.plex_holds) + len(STATE.live) >= STATE.total:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            vcn = self.path[len("/auto/v"):]
            STATE.streams.append(vcn)
            STATE.live.append(vcn)
            maker = _pattern(vcn) if VIDEO else None
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mpeg")
                self.end_headers()
                while True:
                    if maker:
                        chunk = maker.stdout.read(188 * 64)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        continue
                    if not STATE.silent:
                        self.wfile.write(CHUNK * 40)
                        self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass                # the reader hung up, which is how it ends
            finally:
                if maker:
                    maker.kill()
                    maker.wait()
                STATE.live.remove(vcn)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def start(host="127.0.0.1"):
    """Run the fake tuner on a free port. Returns (url, stop)."""
    global _URL
    srv = ThreadingHTTPServer((host, 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _URL = f"http://{host}:{srv.server_address[1]}"
    return _URL, srv.shutdown
