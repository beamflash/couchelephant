"""Tell the home network this server is here (mDNS), so the apps can find it.

The frozen tray and menu bar must never import this: zeroconf has compiled
parts and the tray ships only the light modules."""
import asyncio
import logging
import socket
import threading

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import db, paths
from .version import VERSION

log = logging.getLogger("couchelephant.announce")

SERVICE = "_couchelephant._tcp.local."
# Tests narrow this to loopback. None means every interface.
INTERFACES = None


def machine_name() -> str:
    return socket.gethostname().split(".")[0] or "server"


def instance_name() -> str:
    return f"CouchElephant on {machine_name()}"


def enabled() -> bool:
    return db.get_setting("announce") != "0"


def txt(install_id=None) -> dict:
    """What goes in the record. Never a key, token or password."""
    out = {"id": (install_id or paths.install_id)(), "v": VERSION,
           "name": machine_name(), "tls": "0"}
    # Part 4 adds TLS and sets out["fp"] here when tls is "1".
    return out


class Announcer:
    def __init__(self, install_id=None):
        self._id = install_id
        self._port = None
        self._failed = False
        self._loop = None
        self._thread = None
        self._zc = None
        self._info = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._info is not None

    def start(self, port: int) -> None:
        self._port = port
        self.refresh()

    def refresh(self) -> None:
        """Match the network to the setting and the current record."""
        with self._lock:
            if self._port is None or self._failed:
                return
            try:
                if enabled():
                    self._run(self._announce())
                elif self._info is not None:
                    self._run(self._withdraw())
            except Exception as e:
                self._failed = True
                self._info = None
                self._close()
                log.info("not announcing on the network (%s); the server runs on", e)

    def stop(self) -> None:
        with self._lock:
            self._port = None
            try:
                if self._zc is not None:
                    self._run(self._shutdown())
            except Exception:
                pass
            self._close()

    # The zeroconf work runs on its own loop in a small thread.
    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever,
                                            daemon=True, name="announce")
            self._thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(15)

    def _close(self):
        loop, thread = self._loop, self._thread
        self._loop = self._thread = self._zc = self._info = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(3)

    def _make_info(self):
        t = txt(self._id)
        return ServiceInfo(
            SERVICE, f"{instance_name()}.{SERVICE}", port=self._port,
            properties=t, server=f"{machine_name()}.local.",
            addresses=_addresses())

    async def _announce(self):
        if self._zc is None:
            kw = {"interfaces": INTERFACES} if INTERFACES else {}
            self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only, **kw)
        info = self._make_info()
        if self._info is None:
            # Two installs on one machine share the name; take the next free one.
            await self._zc.async_register_service(info, allow_name_change=True)
        else:
            info.name = self._info.name
            await self._zc.async_update_service(info)
        self._info = info

    async def _withdraw(self):
        await self._zc.async_unregister_service(self._info)
        self._info = None

    async def _shutdown(self):
        if self._info is not None:
            await self._zc.async_unregister_service(self._info)
        await self._zc.async_close()
        # Let the last broadcasts end, so closing the loop leaves nothing pending.
        me = asyncio.current_task()
        for t in asyncio.all_tasks():
            if t is not me:
                t.cancel()
        await asyncio.sleep(0)


def _addresses() -> list:
    """This machine's IPv4 addresses, packed, so the apps need no second lookup."""
    if INTERFACES:
        ips = [i for i in INTERFACES if isinstance(i, str)]
    else:
        import ifaddr
        ips = [ip.ip for ad in ifaddr.get_adapters() for ip in ad.ips
               if ip.is_IPv4 and not ip.ip.startswith("127.")]
    return [socket.inet_aton(i) for i in ips]


_default = Announcer()


def start(port: int) -> None:
    _default.start(port)


def stop() -> None:
    _default.stop()


def refresh() -> None:
    _default.refresh()
