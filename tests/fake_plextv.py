"""plex.tv, as far as the sign-in needs it.

Served over real HTTP on localhost, like fake_plex, so `plextv.py` is run
through httpx. Tests drive it through STATE: `approve()` is the user clicking
Allow on plex.tv, `load_resources()` is the account's server list.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        self.drip = 0           # seconds to dribble an answer out, a byte at a time
        self.fail_status = None  # answer every request with this status
        self.raw = None         # if set, every GET answers this JSON
        self.valid_tokens = {TOKEN}  # account tokens /resources accepts

    def approve(self, pin_id, token=TOKEN):
        if pin_id not in self.pins:
            raise KeyError(f"fake plex.tv has no pin {pin_id}")
        self.pins[pin_id]["authToken"] = token
        self.valid_tokens.add(token)

    def expire(self, pin_id):
        self.pins[pin_id]["expired"] = True

    def load_resources(self, resources):
        self.resources = list(resources)


STATE = State()


def _conn(c):
    """(uri, local, relay) or (uri, local, relay, address, port). Without the
    last two they come from the uri, like a plain http address."""
    from urllib.parse import urlsplit
    uri, local, relay = c[:3]
    u = urlsplit(uri)
    address, port = c[3:5] if len(c) == 5 else (u.hostname, u.port)
    return {"protocol": u.scheme, "address": address, "port": port,
            "uri": uri, "local": local, "relay": relay}


def server(name, client_id, connections, token="srv-token-abc", provides="server",
           owned=True):
    """One entry of /api/v2/resources."""
    return {
        "name": name,
        "clientIdentifier": client_id,
        "provides": provides,
        "accessToken": token,
        "owned": owned,
        "connections": [_conn(c) for c in connections],
    }


def plexdirect(plex_url, host="192-168-1-5"):
    """A local plex.direct style connection that will not resolve, whose plain
    address is the fake Plex. Returns a connection tuple."""
    port = int(plex_url.rsplit(":", 1)[1])
    return (f"https://{host}.abc123.plex.direct.invalid:{port}", True, False,
            "127.0.0.1", port)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if STATE.drip:
            # One byte at a time: no single read stalls, the whole answer does.
            step = STATE.drip / len(raw)
            for i in range(len(raw)):
                self.wfile.write(raw[i:i + 1])
                self.wfile.flush()
                threading.Event().wait(step)
            return
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
        if STATE.raw is not None and method == "GET":
            return self._send(STATE.raw)
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
            if pin is None or pin.get("expired"):
                return self._send({"errors": [{"message": "not found"}]}, 404)
            return self._send({"id": pid, "code": pin["code"],
                               "authToken": pin["authToken"]})
        if method == "GET" and u.path == "/api/v2/resources":
            if self.headers.get("X-Plex-Token") not in STATE.valid_tokens:
                return self._send({"errors": [{"message": "unauthorized"}]}, 401)
            return self._send(STATE.resources)
        return self._send({"error": "gone"}, 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start():
    """Run the fake on a free localhost port. Returns (url, stop)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return f"http://127.0.0.1:{srv.server_address[1]}", stop
