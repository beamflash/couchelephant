"""One port for HTTP and HTTPS.

A TLS handshake starts with the byte 0x16 and no HTTP request does. We accept
each connection ourselves, peek at that first byte without taking it, and hand
the socket to uvicorn's own protocol class, wrapped in TLS or not. The socket
is the real one, so request.client is the true address (app.exposure reads it)
and keep-alive, streaming and graceful shutdown are uvicorn's own.

Why not a protocol that buffers and calls start_tls: uvicorn's protocol would
sit on a transport that is not the socket, and its client address, flow
control and shutdown would all be one step removed.

TLS is offered only while Use away from home is on (app.tls.context)."""
import asyncio
import errno
import logging
import re
import select
import socket
import ssl
import sys
import threading
import time

import uvicorn
from starlette.requests import Request

from . import exposure, tls

log = logging.getLogger("couchelephant.oneport")

FIRST_BYTE_SECONDS = 5      # a client that says nothing is dropped after this
HANDSHAKE_SECONDS = 10
SHUTDOWN_SECONDS = 3         # a TLS close waits for the peer; do not wait long
TLS_HELLO = 0x16
_BAD_HOST = re.compile(r"[/\\?#@\s]")
IS_WINDOWS = sys.platform == "win32"
NO_READER = False            # tests: act like a loop with no add_reader
MAX_WATCHED = 500            # select() on Windows takes 512 at most
_SLEEP_ERRNOS = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS}


class _Watcher:
    """For loops with no add_reader (Windows' proactor loop): one thread
    watches every waiting socket with a single select(). Each socket has its
    own deadline, counted from accept."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []                     # [conn, deadline, loop, future]
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        threading.Thread(target=self._run, daemon=True, name="peek").start()

    def wait(self, loop, conn, deadline):
        fut = loop.create_future()
        with self._lock:
            self._items.append([conn, deadline, loop, fut])
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass
        return fut

    @staticmethod
    def _resolve(item, value):
        _, _, loop, fut = item

        def done():
            if not fut.done():
                fut.set_result(value)
        try:
            loop.call_soon_threadsafe(done)
        except RuntimeError:                 # that loop is gone
            pass

    def _run(self):
        while True:
            now = time.monotonic()
            with self._lock:
                keep, gone = [], []
                for it in self._items:
                    if it[3].done() or it[0].fileno() < 0:
                        continue
                    (gone if it[1] <= now else keep).append(it)
                keep.sort(key=lambda it: it[1])
                self._items = keep + []
            for it in gone:
                self._resolve(it, False)
            watched = keep[:MAX_WATCHED]
            timeout = min([it[1] for it in watched] + [now + 1.0]) - now
            try:
                ready, _, _ = select.select(
                    [it[0] for it in watched] + [self._wake_r], [], [], max(0.0, timeout))
            except (OSError, ValueError):    # one was closed under us; rescan
                time.sleep(0.01)
                continue
            if self._wake_r in ready:
                try:
                    self._wake_r.recv(4096)
                except OSError:
                    pass
            hit = [it for it in watched if it[0] in ready]
            with self._lock:
                self._items = [it for it in self._items if it not in hit]
            for it in hit:
                self._resolve(it, True)


_watcher = None
_watcher_lock = threading.Lock()


def _the_watcher():
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = _Watcher()
        return _watcher


async def _wait_readable(loop, conn, deadline) -> bool:
    """Sleep until the socket can be read, or the deadline. No polling: a
    reader on the loop, or the one select() thread where the loop has none."""
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    fut = None
    if not NO_READER:
        fut = loop.create_future()

        def ready():
            if not fut.done():
                fut.set_result(True)
        try:
            loop.add_reader(conn.fileno(), ready)
        except NotImplementedError:
            fut = None
    if fut is None:
        return bool(await _the_watcher().wait(loop, conn, deadline))
    try:
        await asyncio.wait_for(fut, left)
        return True
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(conn.fileno())


async def peek_first_byte(loop, conn, deadline=None):
    """The first byte of the connection, left in place. None when the client
    sent nothing by the deadline (monotonic, default 5 s from now) or went
    away."""
    if deadline is None:
        deadline = time.monotonic() + FIRST_BYTE_SECONDS
    try:
        data = conn.recv(1, socket.MSG_PEEK)
        return data[0] if data else None
    except (BlockingIOError, InterruptedError):
        pass
    if not await _wait_readable(loop, conn, deadline):
        return None
    try:
        data = conn.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, InterruptedError):
        return None
    return data[0] if data else None


NO_LOWAT = False             # tests: act like a system without SO_RCVLOWAT


async def peek_hello(loop, conn, deadline):
    """The whole first TLS record (the ClientHello), left in place. It may
    arrive in pieces, so peek until the record's own length is there; stop at
    the deadline or at 16 KB and give back what there is (the parser then
    says no, and the connection is served as it always was).

    Waiting costs nothing: the socket is told (SO_RCVLOWAT) not to count as
    readable until the bytes still missing are there, so a thousand slow
    clients are a thousand idle sockets. Where that is not available
    (Windows), a short sleep that doubles to a quarter second stands in."""
    nap = 0.02
    while True:
        try:
            data = conn.recv(5 + tls.MAX_HELLO, socket.MSG_PEEK)
            if not data:
                return b""                  # the client went away
        except (BlockingIOError, InterruptedError):
            data = b""
        except OSError:
            return b""
        need = 5
        if len(data) >= 5:
            want = 5 + int.from_bytes(data[3:5], "big")
            if want > 5 + tls.MAX_HELLO or len(data) >= want:
                return data[:want]
            need = want
        if time.monotonic() >= deadline:
            return data
        if _set_lowat(conn, need):
            try:
                await _wait_readable(loop, conn, deadline)
            finally:
                _set_lowat(conn, 1)
        else:
            await asyncio.sleep(min(nap, max(0.0, deadline - time.monotonic())))
            nap = min(nap * 2, 0.25)


def _set_lowat(conn, n: int) -> bool:
    if NO_LOWAT:
        return False
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVLOWAT, n)
        return True
    except (OSError, AttributeError):
        return False


class _Redirector:
    """Plain HTTP from outside, while away is on, goes to the same URL on
    HTTPS. Home keeps plain HTTP. A request that came
    through a trusted proxy is left alone: the proxy owns the outside."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("scheme") == "http" and self._outside(scope):
            host = ""
            for k, v in scope["headers"]:
                if k == b"host":
                    host = v.decode("latin-1")
            if not host or _BAD_HOST.search(host):
                host = None
            if host:
                path = scope.get("raw_path") or scope["path"].encode()
                url = "https://" + host + path.decode("latin-1")
                if scope.get("query_string"):
                    url += "?" + scope["query_string"].decode("latin-1")
                # 307 for a page load, so a browser never remembers it; 308
                # for the rest, which keeps the method and body.
                status = 307 if scope["method"] in ("GET", "HEAD") else 308
                await send({"type": "http.response.start", "status": status, "headers": [
                    (b"location", url.encode("latin-1")), (b"content-length", b"0"),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _outside(scope) -> bool:
        if not exposure.away_on():
            return False
        request = Request(scope)
        peer = exposure._parse(getattr(request.client, "host", None))
        trusted = exposure._trusted()
        if peer is not None and trusted and exposure._is_trusted(peer, trusted):
            return False
        return not exposure.is_home(request)


_last_accept_log = 0.0
_tls_warned = False


def _accept_failed(e) -> bool:
    """Log (at most every 10 s) and say whether to pause: only when the
    machine is out of descriptors or buffers. A connection that was reset
    before we took it is nothing."""
    global _last_accept_log
    now = time.monotonic()
    if now - _last_accept_log >= 10:
        _last_accept_log = now
        log.warning("accept failed: %s", e)
    return e.errno in _SLEEP_ERRNOS


def _tls_failed(e) -> None:
    """A handshake that failed on our side. Said once, not per connection:
    port scanners and old clients would fill the log."""
    global _tls_warned
    if not _tls_warned:
        _tls_warned = True
        log.warning("a TLS handshake failed (%s); further ones are not logged", e)


def _listening_socket(config, windows=None, make=socket.socket):
    """On Windows another program can bind the same port when SO_REUSEADDR is
    set, so there it is left off and the port is taken exclusively. Elsewhere
    uvicorn's own socket (SO_REUSEADDR, so a restart does not wait)."""
    if not (IS_WINDOWS if windows is None else windows):
        return config.bind_socket()
    host = config.host or "0.0.0.0"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = make(family=family)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind((host, config.port))
    except OSError as exc:
        logging.getLogger("uvicorn.error").error(exc)
        sys.exit(1)
    logging.getLogger("uvicorn.error").info(
        "Uvicorn running on http://%s:%d (Press CTRL+C to quit)", host, sock.getsockname()[1])
    return sock


class _Listener:
    """Stands in for asyncio's Server in uvicorn's shutdown code."""

    def __init__(self, server, sock):
        self.server, self.sock = server, sock
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.pending: set = set()
        sock.setblocking(False)
        sock.listen(server.config.backlog)
        self.task = self.loop.create_task(self._accept())

    async def _accept(self):
        while not self.closed:
            try:
                conn, _ = await self.loop.sock_accept(self.sock)
            except asyncio.CancelledError:
                raise
            except OSError as e:
                if self.closed or self.sock.fileno() < 0:
                    return
                if _accept_failed(e):
                    await asyncio.sleep(0.1)   # out of descriptors: do not spin
                continue
            t = self.loop.create_task(self._hand_over(conn, time.monotonic()))
            self.pending.add(t)
            t.add_done_callback(self.pending.discard)

    async def _hand_over(self, conn, accepted):
        ssl_ctx, was_tls = None, False
        try:
            conn.setblocking(False)
            first = await peek_first_byte(self.loop, conn, accepted + FIRST_BYTE_SECONDS)
            if first is None:
                conn.close()
                return
            if first == TLS_HELLO:
                was_tls = True
                ssl_ctx = tls.context()
                if ssl_ctx is None:             # away is off: TLS is refused
                    conn.close()
                    return
                if tls._challenge:              # an order is running: look at the hello
                    hello = await peek_hello(self.loop, conn, accepted + FIRST_BYTE_SECONDS)
                    ssl_ctx = tls.challenge_for(hello) or ssl_ctx
            await self.loop.connect_accepted_socket(
                self.server.create_protocol, conn, ssl=ssl_ctx,
                ssl_handshake_timeout=HANDSHAKE_SECONDS if ssl_ctx else None,
                ssl_shutdown_timeout=SHUTDOWN_SECONDS if ssl_ctx else None)
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception as e:
            if was_tls and isinstance(e, ssl.SSLError):
                _tls_failed(e)
            else:
                log.debug("connection dropped: %s", e)
            conn.close()

    def close(self):
        self.closed = True
        self.task.cancel()
        for t in list(self.pending):
            t.cancel()
        self.sock.close()

    async def wait_closed(self):
        await asyncio.gather(self.task, *self.pending, return_exceptions=True)


class Server(uvicorn.Server):
    """uvicorn's server with our own listener. Everything else is uvicorn's:
    lifespan, signals, the tick loop, graceful shutdown."""

    def create_protocol(self):
        config = self.config
        return config.http_protocol_class(
            config=config, server_state=self.server_state,
            app_state=self.lifespan.state, _loop=None)

    async def startup(self, sockets=None):
        config = self.config
        if not getattr(self, "_wrapped", False):
            config.loaded_app = _Redirector(config.loaded_app)
            self._wrapped = True
        await self.lifespan.startup()
        if self.lifespan.should_exit:
            self.should_exit = True
            return
        try:
            sock = _listening_socket(config)
        except SystemExit:
            await self.lifespan.shutdown()
            raise
        self.servers = [_Listener(self, sock)]
        self.started = True


def serve(config: uvicorn.Config, server=None) -> None:
    """Run the server. Pass a Server of your own to be able to stop it."""
    (server or Server(config)).run()
