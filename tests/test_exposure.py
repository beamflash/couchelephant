"""Who is at home: the answer the away gate and HTTPS build on."""
import ipaddress
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app import db, exposure


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    monkeypatch.setattr(exposure, "_adapters", lambda: [])
    monkeypatch.setattr(exposure, "_route_files", lambda: [])
    exposure.clear_cache()
    yield
    exposure.clear_cache()


def req(peer, xff=None):
    """xff may be one line or a list of lines."""
    lines = [xff] if isinstance(xff, str) else (xff or [])
    raw = [(b"x-forwarded-for", x.encode()) for x in lines]
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=Headers(raw=raw))


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
    assert exposure.is_home(req("192.168.1.1", ["8.8.8.8", "9.9.9.9"]))


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


def test_trusted_peer_without_a_header_is_outside():
    db.set_setting("trusted_proxies", "192.168.1.1")
    assert not exposure.is_home(req("192.168.1.1"))


def test_every_header_line_is_read():
    db.set_setting("trusted_proxies", "192.168.1.1")
    r = req("192.168.1.1", ["192.168.1.9", "8.8.8.8"])
    assert exposure.client_address(r) == ipaddress.ip_address("8.8.8.8")
    assert not exposure.is_home(r)


def test_mapped_trusted_proxy_entry_is_unwrapped():
    db.set_setting("trusted_proxies", "::ffff:192.168.1.1")
    assert exposure.client_address(req("192.168.1.1", "8.8.8.8")) == ipaddress.ip_address("8.8.8.8")


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


def test_wide_own_subnets_are_skipped(monkeypatch):
    monkeypatch.setattr(exposure, "_adapters", lambda: [
        _nic("eno1", _IP("203.0.113.7", 8), _IP("203.0.113.8", 16),
             _IP(("2001:db8::1", 0, 0), 32), _IP(("2001:db8:1::1", 0, 0), 48))])
    nets = exposure.local_subnets()
    assert ipaddress.ip_network("203.0.0.0/16") in nets
    assert ipaddress.ip_network("2001:db8:1::/48") in nets
    assert len(nets) == 2


def test_local_subnets_are_cached(monkeypatch):
    calls = []

    def adapters():
        calls.append(1)
        return []
    monkeypatch.setattr(exposure, "_adapters", adapters)
    exposure.local_subnets()
    exposure.local_subnets()
    assert len(calls) == 1
    exposure.clear_cache()
    exposure.local_subnets()
    assert len(calls) == 2


def _container(monkeypatch, host_net=False, veth=True, gateways=("172.18.0.1",),
               nics=None):
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(announce, "_host_network", lambda: host_net)
    monkeypatch.setattr(exposure, "_gateways", lambda: {ipaddress.ip_address(g) for g in gateways})
    monkeypatch.setattr(exposure, "_eth0_is_veth", lambda: veth)
    monkeypatch.setattr(exposure, "_adapters", lambda: nics if nics is not None else [
        _nic("lo", _IP("127.0.0.1", 8)), _nic("eth0", _IP("172.18.0.2", 16))])


def test_container_gateway_is_never_home(monkeypatch):
    # The docker gateway is what published ports show as the peer.
    _container(monkeypatch)
    assert not exposure.is_home(req("172.18.0.1"))


def test_container_gateway_outside_the_docker_range_is_still_not_home(monkeypatch):
    _container(monkeypatch, veth=False, gateways=("192.168.1.1",),
               nics=[_nic("eth0", _IP("192.168.1.50", 24))])
    assert not exposure.is_home(req("192.168.1.1"))
    assert exposure.is_home(req("192.168.1.77"))


def test_veth_container_subnet_is_not_home_even_at_a_lan_address(monkeypatch):
    _container(monkeypatch, gateways=("192.168.64.1",),
               nics=[_nic("eth0", _IP("192.168.64.2", 24))])
    assert not exposure.is_home(req("192.168.64.9"))
    assert exposure.is_home(req("192.168.1.9"))


def test_unknown_driver_is_the_strict_side(monkeypatch):
    _container(monkeypatch, veth=None, gateways=("192.168.64.1",),
               nics=[_nic("eth0", _IP("192.168.64.2", 24))])
    assert not exposure.is_home(req("192.168.64.9"))


def test_macvlan_keeps_the_lan_home_but_not_the_router(monkeypatch):
    _container(monkeypatch, veth=False, gateways=("192.168.1.1",),
               nics=[_nic("eth0", _IP("192.168.1.50", 24))])
    assert exposure.is_home(req("192.168.1.9"))
    assert not exposure.is_home(req("192.168.1.1"))


def test_host_network_docker_subnets_are_not_home(monkeypatch):
    from app import paths
    monkeypatch.setattr(paths, "_in_container", lambda: False)
    monkeypatch.setattr(exposure, "_adapters", lambda: [
        _nic("eno1", _IP("192.168.1.5", 24)),
        _nic("docker0", _IP("172.17.0.1", 16)),
        _nic("br-1a2b", _IP("10.88.0.1", 16)),
        _nic("veth9", _IP("172.19.0.1", 16))])
    assert not exposure.is_home(req("172.17.0.5"))
    assert not exposure.is_home(req("10.88.3.4"))
    assert not exposure.is_home(req("172.19.0.7"))
    assert exposure.is_home(req("192.168.1.9"))
    assert exposure.is_home(req("172.20.0.5"))


def test_host_network_container_uses_the_host_rules(monkeypatch):
    _container(monkeypatch, host_net=True, nics=[
        _nic("eno1", _IP("192.168.1.5", 24)), _nic("docker0", _IP("172.17.0.1", 16))])
    assert not exposure.is_home(req("172.17.0.5"))
    assert exposure.is_home(req("172.18.0.1"))
    assert exposure.is_home(req("192.168.1.9"))


def test_gateways_read_from_the_proc_files(monkeypatch, tmp_path):
    v4 = tmp_path / "route"
    v4.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                  "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
                  "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n")
    v6 = tmp_path / "ipv6_route"
    v6.write_text("00000000000000000000000000000000 00 " + "0" * 32 + " 00 "
                  "fe800000000000000000000000000001 00000400 00000001 00000000 00000003 eth0\n"
                  "fe800000000000000000000000000000 40 " + "0" * 32 + " 00 "
                  + "0" * 32 + " 00000100 00000001 00000000 00000001 eth0\n")
    monkeypatch.setattr(exposure, "_route_files", lambda: [str(v4), str(v6)])
    exposure.clear_cache()
    assert exposure._gateways() == {ipaddress.ip_address("172.18.0.1"),
                                    ipaddress.ip_address("fe80::1")}


def test_missing_route_files_give_no_gateways(monkeypatch):
    monkeypatch.setattr(exposure, "_route_files", lambda: ["/nonexistent/route"])
    assert exposure._gateways() == set()


def test_docker_facts_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(exposure, "_route_files", lambda: calls.append(1) or [])
    from app import announce, paths
    monkeypatch.setattr(paths, "_in_container", lambda: True)
    monkeypatch.setattr(announce, "_host_network", lambda: False)
    exposure.is_home(req("10.0.0.1"))
    exposure.is_home(req("10.0.0.1"))
    assert len(calls) == 1


def test_the_server_config_ignores_forwarding_headers(monkeypatch):
    # uvicorn's own proxy handling would rewrite request.client first.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    from app import __main__ as entry
    cfg = entry.make_config(8123)
    assert cfg.proxy_headers is False
    cfg.load()
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app = cfg.loaded_app
    while app is not None:
        assert not isinstance(app, ProxyHeadersMiddleware)
        app = getattr(app, "app", None)


@pytest.mark.parametrize("driver,want", [("veth", True), ("macvlan", False),
                                         ("ipvlan", False), ("e1000e", None),
                                         (None, None)])
def test_eth0_driver_decides_bridge_or_lan(monkeypatch, driver, want):
    # Only a confirmed macvlan or ipvlan counts as being on the LAN. A real
    # Docker veth has no DEVTYPE in uevent, so that file cannot tell them apart.
    monkeypatch.setattr(exposure, "_eth0_driver", lambda: driver)
    assert exposure._eth0_is_veth() is want
