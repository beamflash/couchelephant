"""The server announces itself on the home network."""
import logging
import socket
import threading
import time

import pytest
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from app import announce, db

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
    monkeypatch.setattr(announce, "refresh", lambda: calls.append(1))
    settings.apply({"announce": "0"})
    settings.apply({"announce": "0"})
    settings.apply({"announce": "1"})
    assert len(calls) == 2
    assert db.get_setting("announce") == "1"


def test_tray_does_not_import_announce():
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, app.tray; print('app.announce' in sys.modules or 'zeroconf' in sys.modules)"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"
