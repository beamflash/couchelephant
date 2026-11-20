"""Who is at home: the answer the away gate and HTTPS build on."""
import ipaddress
from types import SimpleNamespace

import pytest

from app import db, exposure


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    monkeypatch.setattr(exposure, "_adapters", lambda: [])


def req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.9",
                                "192.168.1.5", "169.254.1.1", "::1", "fd00::1",
                                "fe80::1", "::ffff:192.168.1.5"])
def test_home_addresses(ip):
    assert exposure.is_home(req(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "2001:4860::8888", "100.64.0.1",
                                "::ffff:8.8.8.8", "172.32.0.1"])
def test_outside_addresses(ip):
    assert not exposure.is_home(req(ip))


def test_xff_ignored_from_an_untrusted_peer():
    assert exposure.is_home(req("192.168.1.1", "8.8.8.8"))
    assert exposure.client_address(req("192.168.1.1", "8.8.8.8")) == ipaddress.ip_address("192.168.1.1")


def test_xff_counts_from_a_trusted_proxy():
    db.set_setting("trusted_proxies", "192.168.1.1")
    assert not exposure.is_home(req("192.168.1.1", "8.8.8.8"))
    assert exposure.is_home(req("192.168.1.1", "192.168.1.50"))


def test_trusted_proxies_take_cidrs_and_spaces():
    db.set_setting("trusted_proxies", "10.0.0.0/8, 192.168.1.1")
    assert exposure.client_address(req("10.9.9.9", "8.8.8.8")) == ipaddress.ip_address("8.8.8.8")


def test_chain_takes_right_most_non_proxy():
    db.set_setting("trusted_proxies", "192.168.1.1,10.0.0.2")
    r = req("192.168.1.1", "1.2.3.4, 8.8.8.8, 10.0.0.2")
    assert exposure.client_address(r) == ipaddress.ip_address("8.8.8.8")
    # A spoofed left part cannot hide the real outside client.
    assert not exposure.is_home(req("192.168.1.1", "192.168.1.9, 8.8.8.8"))


def test_all_proxies_in_chain_gives_the_leftmost():
    db.set_setting("trusted_proxies", "192.168.1.1,10.0.0.2")
    assert exposure.client_address(req("192.168.1.1", "10.0.0.2")) == ipaddress.ip_address("10.0.0.2")


@pytest.mark.parametrize("xff", ["junk", "8.8.8.8, junk", "", "8.8.8.8,,", "1.2.3.4:80"])
def test_junk_xff_is_outside_not_an_error(xff):
    db.set_setting("trusted_proxies", "192.168.1.1")
    assert not exposure.is_home(req("192.168.1.1", xff))


def test_junk_peer_is_outside_and_junk_proxy_entries_are_skipped():
    db.set_setting("trusted_proxies", "nonsense, 192.168.1.1")
    assert not exposure.is_home(req("not-an-ip"))
    assert not exposure.is_home(SimpleNamespace(client=None, headers={}))
    assert not exposure.is_home(req("192.168.1.1", "8.8.8.8"))


def test_mapped_proxy_and_mapped_xff():
    db.set_setting("trusted_proxies", "192.168.1.1")
    assert exposure.client_address(req("::ffff:192.168.1.1", "::ffff:8.8.8.8")) == ipaddress.ip_address("8.8.8.8")


def test_away_defaults_off():
    assert exposure.away_on() is False
    db.set_setting("away_mode", "1")
    assert exposure.away_on() is True
    db.set_setting("away_mode", "0")
    assert exposure.away_on() is False


class _IP:
    def __init__(self, ip, n):
        self.ip, self.network_prefix = ip, n


def _nic(name, *ips):
    return SimpleNamespace(nice_name=name, ips=list(ips))


def test_local_subnets_use_each_prefix_and_skip_docker(monkeypatch):
    monkeypatch.setattr(exposure, "_adapters", lambda: [
        _nic("eno1", _IP("203.0.113.7", 28), _IP(("fe80::1", 0, 2), 64)),
        _nic("docker0", _IP("172.17.0.1", 16)),
        _nic("br-abc", _IP("172.18.0.1", 16)),
        _nic("veth12", _IP("172.19.0.1", 16)),
    ])
    nets = exposure.local_subnets()
    assert ipaddress.ip_network("203.0.113.0/28") in nets
    assert ipaddress.ip_network("fe80::/64") in nets
    assert not any(n.network_address == ipaddress.ip_address("172.17.0.0") for n in nets)
    assert len(nets) == 2


def test_own_subnet_counts_as_home(monkeypatch):
    monkeypatch.setattr(exposure, "_adapters", lambda: [_nic("eno1", _IP("203.0.113.7", 28))])
    assert exposure.is_home(req("203.0.113.9"))
    assert not exposure.is_home(req("203.0.113.40"))


def test_container_bridge_subnet_is_left_out(monkeypatch):
    from app import paths
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(exposure, "_adapters", lambda: [_nic("eth0", _IP("172.18.0.2", 16))])
    assert exposure.local_subnets() == []


def test_adapter_failure_gives_no_subnets(monkeypatch):
    def boom():
        raise OSError("no")
    monkeypatch.setattr(exposure, "_adapters", boom)
    assert exposure.local_subnets() == []
    assert exposure.is_home(req("10.0.0.1"))
