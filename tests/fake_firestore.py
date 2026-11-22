"""The slice of Firestore the report sender talks to, over real HTTP.

It enforces the rules the real project has: create only at the documented
path, exactly the documented fields, and no reads. Tests drive it with STATE.
"""
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FIELDS = {"reportId": "stringValue", "sealed": "bytesValue", "size": "integerValue",
          "created": "timestampValue", "source": "stringValue",
          "appVersion": "stringValue", "keyId": "stringValue",
          "installId": "stringValue"}
PROJECT = "test-project"
API_KEY = "test-api-key"


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.docs = {}        # id -> fields dict
        self.bodies = []      # raw request bodies, for "no cleartext" checks
        self.requests = []    # (method, path, query)
        self._fail = []       # statuses to answer, in order

    def fail_next(self, status, n=1):
        self._fail += [status] * n


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body=None):
        raw = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        STATE.requests.append(("GET", self.path, {}))
        self._send(403, {"error": {"status": "PERMISSION_DENIED"}})

    def do_POST(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        STATE.requests.append(("POST", u.path, q))
        STATE.bodies.append(raw)
        if STATE._fail:
            return self._send(STATE._fail.pop(0))
        want = f"/v1/projects/{PROJECT}/databases/(default)/documents/reports"
        if u.path != want or q.get("key") != API_KEY or not q.get("documentId"):
            return self._send(403)
        try:
            fields = json.loads(raw)["fields"]
        except (ValueError, KeyError, TypeError):
            return self._send(400)
        if set(fields) != set(FIELDS):
            return self._send(400)
        for k, kind in FIELDS.items():
            if not isinstance(fields[k], dict) or set(fields[k]) != {kind}:
                return self._send(400)
        if fields["reportId"]["stringValue"] != q["documentId"]:
            return self._send(400)
        base64.b64decode(fields["sealed"]["bytesValue"], validate=True)
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", fields["created"]["timestampValue"]):
            return self._send(400)
        if q["documentId"] in STATE.docs:
            return self._send(409, {"error": {"status": "ALREADY_EXISTS"}})
        STATE.docs[q["documentId"]] = fields
        self._send(200, {"name": "x"})


def start():
    """Run the fake on a free localhost port. Returns (url, stop)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return f"http://127.0.0.1:{srv.server_address[1]}", stop
