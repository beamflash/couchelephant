"""Tell the home network this server is here (mDNS), so the apps can find it.

The frozen tray and menu bar must never import this: zeroconf has compiled
parts and the tray ships only the light modules."""
import asyncio
import ipaddress
import logging
import re
import socket
import threading
import time

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import db, paths
from .version import VERSION

log = logging.getLogger("couchelephant.announce")

SERVICE = "_couchelephant._tcp.local."
# Tests narrow this to loopback. None means every interface.
INTERFACES = None
# After a failure, try again no sooner than this.
RETRY_SECONDS = 300


def machine_name() -> str:
    return socket.gethostname().split(".")[0] or "server"


def instance_name() -> str:
    return f"CouchElephant on {machine_name()}"


def host_label(install_id=None) -> str:
    """Our own .local name. Never the machine's: avahi or mDNSResponder owns
    that, and two owners make it rename the host."""
    raw = (install_id or paths.install_id)()
    return "couchelephant-" + (re.sub(r"[^a-z0-9-]+", "-", str(raw).lower()).strip("-") or "x")


def is_bridge(ip: str) -> bool:
    """In a container, 172.16.0.0/12 is Docker's own network (compose makes
    172.18 and up), which the home network cannot reach."""
    try:
        return paths._in_container() and ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12")
    except ValueError:
        return False


def enabled() -> bool:
    return db.get_setting("announce") != "0"


def txt(install_id=None) -> dict:
    """What goes in the record. Never a key, token or password."""
    out = {"id": (install_id or paths.install_id)(), "v": VERSION,
           "name": machine_name(), "tls": "0"}
    # Part 4 adds TLS and sets out["fp"] here when tls is "1".
    return out


class _NoAddress(Exception):
    pass


class Announcer:
    def __init__(self, install_id=None):
        self._id = install_id
        self._port = None
        self._failed = False       # the last try failed; check() retries slowly
        self._logged = False       # one log line per failure, not one per retry
        self._last_try = 0.0
        self._sig = None           # what the live announcement was built from
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

    def refresh(self, retry: bool = False) -> None:
        """Match the network to the setting and the current record. A toggle of
        the setting passes retry=True, which lifts an earlier failure."""
        with self._lock:
            if self._port is None:
                return
            if retry:
                self._failed = False
            if self._failed:
                return
            self._last_try = time.time()
            try:
                if not enabled():
                    if self._info is not None:
                        self._run(self._withdraw())
                    self._sig = None
                    return
                if self._info is not None and self._sig != _signature():
                    self._run(self._withdraw())
                self._run(self._announce())
                self._failed = self._logged = False
            except Exception as e:
                self._fail(e)

    def check(self) -> None:
        """Cheap, run now and then: re-register if the addresses or the machine
        name moved, and try again after a failure, at most every few minutes."""
        if self._port is None or not enabled():
            return
        if self._failed:
            if time.time() - self._last_try >= RETRY_SECONDS:
                self.refresh(retry=True)
        elif self._info is not None and self._sig != _signature():
            self.refresh()

    def _fail(self, e):
        self._failed = True
        self._info = self._sig = None
        try:
            if self._zc is not None and self._loop is not None:
                self._run(self._zc.async_close())
        except Exception as err:
            log.debug("announce close: %s", err)
        self._close()
        if self._logged:
            return
        self._logged = True
        if isinstance(e, _NoAddress):
            log.info("Not announcing on the network: this container only has a Docker "
                     "bridge address. Use host networking to let the apps find it, "
                     "or type the address.")
        else:
            log.info("not announcing on the network (%s); the server runs on", e)

    def stop(self) -> None:
        with self._lock:
            self._port = None
            try:
                if self._zc is not None:
                    self._run(self._shutdown())
            except Exception as e:
                log.debug("announce stop: %s", e)
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
            if not loop.is_running():
                loop.close()

    def _make_info(self, addresses):
        t = txt(self._id)
        return ServiceInfo(
            SERVICE, f"{instance_name()}.{SERVICE}", port=self._port,
            properties=t, server=f"{host_label(self._id)}.local.",
            addresses=list(addresses))

    async def _announce(self):
        sig = _signature()
        if not sig[0] and paths._in_container():
            raise _NoAddress()
        if self._zc is None:
            kw = {"interfaces": INTERFACES} if INTERFACES else {}
            self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only, **kw)
        info = self._make_info(sig[0])
        if self._info is None:
            # Two installs on one machine share the name; take the next free one.
            await self._zc.async_register_service(info, allow_name_change=True)
        else:
            info.name = self._info.name
            await self._zc.async_update_service(info)
        self._info = info
        self._sig = sig

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
    if paths._in_container():
        # Docker's bridges: addresses the home network cannot reach.
        ips = [i for i in ips if not is_bridge(i)]
        if not ips:
            return []
    return [socket.inet_aton(i) for i in ips]


def _signature():
    """(addresses, machine name): when this changes the record is stale."""
    return (tuple(sorted(_addresses())), machine_name())


_default = Announcer()


def start(port: int) -> None:
    _default.start(port)


def stop() -> None:
    _default.stop()


def refresh(retry: bool = False) -> None:
    _default.refresh(retry)

def check() -> None:
    _default.check()
