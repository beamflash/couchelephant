"""The server announces itself on the home network."""
import logging
import socket
import threading
import time

import pytest
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from app import announce, db


@pytest.fixture(autouse=True)
def _outside_a_container(monkeypatch):
    # CI runs the suite inside a container. Tests that want the container
    # case set it themselves.
    from app import paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)

TYPE = "_couchelephant._tcp.local."


def test_txt_has_the_four_keys_and_no_fp():
    t = announce.txt()
    assert t["id"] == announce.paths.install_id()
    assert t["v"] == announce.VERSION
    assert t["name"] == socket.gethostname().split(".")[0]
    assert t["tls"] == "0"
    assert "fp" not in t


def test_txt_carries_no_secret():
    joined = " ".join(announce.txt().values()).lower()
    for word in ("token", "key", "password", "secret"):
        assert word not in joined


def test_instance_name_uses_the_short_machine_name():
    assert announce.instance_name() == (
        "CouchElephant on " + socket.gethostname().split(".")[0])


class _Seen(ServiceListener):
    def __init__(self, zc):
        self.zc, self.found, self.lock = zc, {}, threading.Lock()

    def _grab(self, name):
        info = self.zc.get_service_info(TYPE, name, 3000)
        if info:
            with self.lock:
                self.found[name] = info

    def add_service(self, zc, type_, name):
        self._grab(name)

    def update_service(self, zc, type_, name):
        self._grab(name)

    def remove_service(self, zc, type_, name):
        with self.lock:
            self.found.pop(name, None)


def _wait(fn, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def _multicast_works():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                     socket.inet_aton("127.0.0.1"))
        s.sendto(b"x", ("224.0.0.251", 5353))
        s.close()
        return True
    except OSError:
        return False


needs_multicast = pytest.mark.skipif(
    not _multicast_works(), reason="no multicast route on loopback here")


@pytest.fixture
def browser():
    zc = Zeroconf(interfaces=["127.0.0.1"])
    seen = _Seen(zc)
    b = ServiceBrowser(zc, TYPE, seen)
    yield seen
    b.cancel()
    zc.close()


def _mine(seen, port):
    with seen.lock:
        return [i for i in seen.found.values() if i.port == port]


@needs_multicast
def test_announced_and_withdrawn(browser):
    announce.INTERFACES = ["127.0.0.1"]
    a = announce.Announcer()
    try:
        a.start(18771)
        assert _wait(lambda: _mine(browser, 18771))
        info = _mine(browser, 18771)[0]
        props = {k.decode(): v.decode() for k, v in info.properties.items()}
        assert props == announce.txt()
        db.set_setting("announce", "0")
        a.refresh()
        assert _wait(lambda: not _mine(browser, 18771))
        db.set_setting("announce", "1")
        a.refresh()
        assert _wait(lambda: _mine(browser, 18771))
    finally:
        a.stop()
        announce.INTERFACES = None


@needs_multicast
def test_two_installs_are_both_listed(browser):
    announce.INTERFACES = ["127.0.0.1"]
    one = announce.Announcer()
    two = announce.Announcer(install_id=lambda: "second-install")
    try:
        one.start(18772)
        assert _wait(lambda: _mine(browser, 18772))
        two.start(18773)
        assert _wait(lambda: _mine(browser, 18773))
        assert _mine(browser, 18772)
        ids = {i.properties[b"id"].decode()
               for i in (_mine(browser, 18772) + _mine(browser, 18773))}
        assert len(ids) == 2
    finally:
        one.stop()
        two.stop()
        announce.INTERFACES = None


def test_off_setting_announces_nothing():
    db.set_setting("announce", "0")
    a = announce.Announcer()
    a.start(18774)
    assert not a.running()
    a.stop()


def test_refresh_before_start_is_harmless():
    announce.Announcer().refresh()


def test_failure_logs_one_line_and_does_not_raise(caplog, monkeypatch):
    def boom(*a, **k):
        raise OSError("no multicast")
    monkeypatch.setattr(announce, "AsyncZeroconf", boom)
    a = announce.Announcer()
    with caplog.at_level(logging.INFO, logger="couchelephant.announce"):
        a.start(18775)
        a.refresh()
    a.stop()
    assert not a.running()
    lines = [r for r in caplog.records if r.name == "couchelephant.announce"]
    assert len(lines) == 1


def test_setting_change_calls_refresh(monkeypatch):
    from app.routes import settings
    calls = []
    monkeypatch.setattr(announce, "refresh", lambda retry=False: calls.append(retry))
    settings.apply({"announce": "0"})
    settings.apply({"announce": "0"})
    settings.apply({"announce": "1"})
    assert calls == [True, True]
    assert db.get_setting("announce") == "1"


def test_tray_does_not_import_announce():
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, app.tray; print('app.announce' in sys.modules or 'zeroconf' in sys.modules)"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"


def _fake_zc(monkeypatch):
    """A stand-in for zeroconf that records what it was asked, with no network."""
    seen = {"registered": [], "unregistered": 0}

    class Fake:
        def __init__(self, *a, **k):
            pass

        async def async_register_service(self, info, allow_name_change=False):
            seen["registered"].append(info)

        async def async_update_service(self, info):
            seen["registered"].append(info)

        async def async_unregister_service(self, info):
            seen["unregistered"] += 1

        async def async_close(self):
            pass
    monkeypatch.setattr(announce, "AsyncZeroconf", Fake)
    return seen


def test_failed_start_is_retried_after_the_wait(monkeypatch, caplog):
    seen = _fake_zc(monkeypatch)
    real = announce.AsyncZeroconf
    tries = []

    def flaky(*a, **k):
        tries.append(1)
        if len(tries) == 1:
            raise OSError("network not up yet")
        return real(*a, **k)
    monkeypatch.setattr(announce, "AsyncZeroconf", flaky)
    a = announce.Announcer()
    with caplog.at_level(logging.INFO, logger="couchelephant.announce"):
        a.start(18776)
        assert not a.running()
        a.check()                      # too soon: nothing happens
        assert not a.running()
        monkeypatch.setattr(announce, "RETRY_SECONDS", 0)
        a.check()
    try:
        assert a.running()
        assert len(seen["registered"]) == 1
        assert len([r for r in caplog.records
                    if r.name == "couchelephant.announce"]) == 1
    finally:
        a.stop()


def test_toggling_the_setting_lifts_a_failure(monkeypatch):
    _fake_zc(monkeypatch)
    a = announce.Announcer()
    a._failed = True
    a.start(18777)
    assert not a.running()
    a.refresh(retry=True)
    try:
        assert a.running()
    finally:
        a.stop()


def test_new_address_re_registers(monkeypatch):
    seen = _fake_zc(monkeypatch)
    addrs = [[socket.inet_aton("192.0.2.10")]]
    monkeypatch.setattr(announce, "_addresses", lambda: addrs[0])
    a = announce.Announcer()
    a.start(18778)
    try:
        a.check()
        assert len(seen["registered"]) == 1        # nothing moved
        addrs[0] = [socket.inet_aton("192.0.2.77")]
        a.check()
        assert len(seen["registered"]) == 2
        assert seen["unregistered"] == 1
        assert seen["registered"][1].addresses == [socket.inet_aton("192.0.2.77")]
    finally:
        a.stop()


def test_docker_bridge_address_is_left_out(monkeypatch):
    import ifaddr

    from app import paths

    class Ip:
        def __init__(self, ip):
            self.ip, self.is_IPv4 = ip, True

    class Ad:
        ips = [Ip("172.17.0.2"), Ip("192.0.2.5")]
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [Ad()])
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert announce._addresses() == [socket.inet_aton("192.0.2.5")]


def test_container_on_the_bridge_alone_does_not_announce(monkeypatch, caplog):
    import ifaddr

    from app import paths

    class Ip:
        ip, is_IPv4 = "172.17.0.2", True

    class Ad:
        ips = [Ip()]
    seen = _fake_zc(monkeypatch)
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [Ad()])
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    a = announce.Announcer()
    with caplog.at_level(logging.INFO, logger="couchelephant.announce"):
        a.start(18779)
        a.refresh()
    a.stop()
    assert not seen["registered"]
    lines = [r.getMessage() for r in caplog.records
             if r.name == "couchelephant.announce"]
    assert lines == ["Not announcing on the network: this container only has a Docker "
                     "bridge address. Use host networking to let the apps find it, "
                     "or type the address."]


def test_startup_does_not_wait_for_a_slow_announce(monkeypatch):
    import asyncio

    from app import web
    monkeypatch.delenv("COUCHELEPHANT_NO_SYNC_LOOP", raising=False)
    for name in ("sync_loop", "backup_loop", "outside_loop", "recorder_loop",
                 "backingstore_loop", "announce_loop"):
        monkeypatch.setattr(web, name, lambda: asyncio.sleep(0), raising=False)
    monkeypatch.setattr(web.updates, "update_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web.announce, "start", lambda port: time.sleep(1.0))

    async def go():
        ticks = []

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.05)
                ticks.append(1)
        t = asyncio.create_task(ticker())
        began = time.time()
        await web.startup()
        took = time.time() - began
        await t
        return took, len(ticks)
    took, ticks = asyncio.run(go())
    # The loop kept turning while start slept, so it ran off the loop.
    assert ticks == 5
    assert took >= 0.9


def test_the_service_never_claims_the_machines_own_name(monkeypatch):
    seen = _fake_zc(monkeypatch)
    monkeypatch.setattr(announce, "_addresses", lambda: [socket.inet_aton("192.0.2.10")])
    a = announce.Announcer(install_id=lambda: "ABC_123-def")
    a.start(18780)
    try:
        info = seen["registered"][0]
        assert info.server == "couchelephant-abc-123-def.local."
        assert socket.gethostname().split(".")[0].lower() + ".local." != info.server
        assert info.properties[b"name"].decode() == announce.machine_name()
        assert info.name.startswith("CouchElephant on ")
    finally:
        a.stop()


def test_a_compose_bridge_is_left_out_in_a_container(monkeypatch):
    import ifaddr

    from app import paths

    class Ip:
        def __init__(self, ip):
            self.ip, self.is_IPv4 = ip, True

    class Ad:
        ips = [Ip("172.18.0.2"), Ip("172.31.0.9"), Ip("192.0.2.5")]
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [Ad()])
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    assert announce._addresses() == [socket.inet_aton("192.0.2.5")]
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    assert len(announce._addresses()) == 3


def test_no_zeroconf_is_made_without_an_address_and_a_failure_closes_it(monkeypatch):
    from app import paths
    made, closed = [], []

    class Fake:
        def __init__(self, *a, **k):
            made.append(1)

        async def async_register_service(self, info, allow_name_change=False):
            raise OSError("no route")

        async def async_close(self):
            closed.append(1)
    monkeypatch.setattr(announce, "AsyncZeroconf", Fake)
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(announce, "_addresses", lambda: [])
    a = announce.Announcer()
    a.start(18781)
    assert made == []
    monkeypatch.setattr(announce, "_addresses", lambda: [socket.inet_aton("192.0.2.10")])
    a.refresh(retry=True)
    a.stop()
    assert made == [1] and closed == [1]
