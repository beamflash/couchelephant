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
import concurrent.futures
import logging
import re
import select
import socket
import threading

import uvicorn
from starlette.requests import Request

from . import exposure, tls

log = logging.getLogger("couchelephant.oneport")

FIRST_BYTE_SECONDS = 5      # a client that says nothing is dropped after this
HANDSHAKE_SECONDS = 10
SHUTDOWN_SECONDS = 3         # a TLS close waits for the peer; do not wait long
TLS_HELLO = 0x16
_BAD_HOST = re.compile(r"[/?#@\s]")
_pool = None
_pool_lock = threading.Lock()


def _peek_pool():
    # Only for event loops with no add_reader (Windows' proactor loop).
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(32, "peek")
        return _pool


def _peek_blocking(conn) -> bytes:
    if not select.select([conn], [], [], FIRST_BYTE_SECONDS)[0]:
        return b""
    return conn.recv(1, socket.MSG_PEEK)


async def peek_first_byte(loop, conn):
    """The first byte of the connection, left in place. None when the client
    sent nothing in time or went away."""
    try:
        data = conn.recv(1, socket.MSG_PEEK)
        return data[0] if data else None
    except (BlockingIOError, InterruptedError):
        pass
    fut = loop.create_future()

    def ready():
        if not fut.done():
            fut.set_result(None)
    try:
        loop.add_reader(conn.fileno(), ready)
    except NotImplementedError:
        data = await loop.run_in_executor(_peek_pool(), _peek_blocking, conn)
        return data[0] if data else None
    try:
        await asyncio.wait_for(fut, FIRST_BYTE_SECONDS)
    except TimeoutError:
        return None
    finally:
        loop.remove_reader(conn.fileno())
    data = conn.recv(1, socket.MSG_PEEK)
    return data[0] if data else None


class _Redirector:
    """Plain HTTP from outside, while away is on, goes to the same URL on
    HTTPS (308 keeps the method). Home keeps plain HTTP. A request that came
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
                await send({"type": "http.response.start", "status": 308, "headers": [
                    (b"location", url.encode("latin-1")), (b"content-length", b"0"),
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
                log.warning("accept failed: %s", e)
                await asyncio.sleep(0.1)       # out of descriptors: do not spin
                continue
            t = self.loop.create_task(self._hand_over(conn))
            self.pending.add(t)
            t.add_done_callback(self.pending.discard)

    async def _hand_over(self, conn):
        try:
            conn.setblocking(False)
            first = await peek_first_byte(self.loop, conn)
            if first is None:
                conn.close()
                return
            ssl_ctx = None
            if first == TLS_HELLO:
                ssl_ctx = tls.context()
                if ssl_ctx is None:             # away is off: TLS is refused
                    conn.close()
                    return
            await self.loop.connect_accepted_socket(
                self.server.create_protocol, conn, ssl=ssl_ctx,
                ssl_handshake_timeout=HANDSHAKE_SECONDS if ssl_ctx else None,
                ssl_shutdown_timeout=SHUTDOWN_SECONDS if ssl_ctx else None)
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception as e:
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
            sock = config.bind_socket()
        except SystemExit:
            await self.lifespan.shutdown()
            raise
        self.servers = [_Listener(self, sock)]
        self.started = True


def serve(config: uvicorn.Config, server=None) -> None:
    """Run the server. Pass a Server of your own to be able to stop it."""
    (server or Server(config)).run()
