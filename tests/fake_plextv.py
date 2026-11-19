"""plex.tv, as far as the sign-in needs it.

Served over real HTTP on localhost, like fake_plex, so `plextv.py` is run
through httpx. Tests drive it through STATE: `approve()` is the user clicking
Allow on plex.tv, `load_resources()` is the account's server list.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "tok-secret-0123456789"


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.next_id = 1000
        self.pins = {}          # id -> {"code": str, "authToken": str | None}
        self.requests = []      # (method, path, query dict, headers dict)
        self.resources = []
        self.hang = 0           # how many requests stall before answering
        self.fail_status = None  # answer every request with this status

    def approve(self, pin_id, token=TOKEN):
        self.pins[pin_id]["authToken"] = token

    def load_resources(self, resources):
        self.resources = list(resources)


STATE = State()


def server(name, client_id, connections, token="srv-token-abc", provides="server"):
    """One entry of /api/v2/resources. Each connection is (uri, local, relay)."""
    return {
        "name": name,
        "clientIdentifier": client_id,
        "provides": provides,
        "accessToken": token,
        "connections": [{"uri": u, "local": loc, "relay": rel}
                        for u, loc, rel in connections],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        from urllib.parse import parse_qs, urlsplit
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode()) if n else {}
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        query.update({k: v[0] for k, v in form.items()})
        STATE.requests.append((method, u.path, query, dict(self.headers)))
        if STATE.hang > 0:
            STATE.hang -= 1
            threading.Event().wait(2)
        if STATE.fail_status:
            return self._send({"errors": [{"message": "nope"}]}, STATE.fail_status)
        if method == "POST" and u.path == "/api/v2/pins":
            pid = STATE.next_id
            STATE.next_id += 1
            STATE.pins[pid] = {"code": "ABCD", "authToken": None}
            return self._send({"id": pid, "code": "ABCD",
                               "expiresIn": 900, "authToken": None}, 201)
        if method == "GET" and u.path.startswith("/api/v2/pins/"):
            pid = int(u.path.rsplit("/", 1)[1])
            pin = STATE.pins.get(pid)
            if pin is None:
                return self._send({"errors": [{"message": "not found"}]}, 404)
            return self._send({"id": pid, "code": pin["code"],
                               "authToken": pin["authToken"]})
        if method == "GET" and u.path == "/api/v2/resources":
            return self._send(STATE.resources)
        return self._send({"error": "gone"}, 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start():
    """Run the fake on a free localhost port. Returns (url, stop)."""
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown
