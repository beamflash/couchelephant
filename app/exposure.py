"""Who is at home. The away gate, HTTPS and app pinning all ask this.

Home means loopback, RFC 1918, link-local, IPv6 ULA, and the subnets this
machine sits on. Everything else is outside, including the router's own
public address (hairpin NAT)."""
import functools
import ipaddress
import logging
import socket
import struct
import time

from . import announce, db, paths

log = logging.getLogger("couchelephant.exposure")

_HOME = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


CACHE_SECONDS = 60
_cache: dict = {}


def clear_cache() -> None:
    _cache.clear()


def _cached(fn):
    """Interfaces and routes change rarely; ask at most once a minute."""
    @functools.wraps(fn)
    def wrapper():
        hit = _cache.get(fn.__name__)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = fn()
        _cache[fn.__name__] = (time.monotonic(), value)
        return value
    return wrapper


_ETH0_UEVENT = "/sys/class/net/eth0/uevent"


def _adapters():
    import ifaddr
    return ifaddr.get_adapters()


def _route_files() -> list:
    return ["/proc/net/route", "/proc/net/ipv6_route"]


@_cached
def _gateways() -> set:
    """Default gateways, from the kernel's route tables."""
    out = set()
    for path in _route_files():
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        for line in lines:
            f = line.split()
            try:
                if len(f) >= 8 and f[1] == "00000000" and f[7] == "00000000":
                    gw = int(f[2], 16)
                    if gw:
                        out.add(ipaddress.ip_address(
                            socket.inet_ntoa(struct.pack("<L", gw))))
                elif len(f) >= 10 and f[0] == "0" * 32 and f[1] == "00":
                    gw = ipaddress.ip_address(bytes.fromhex(f[4]))
                    if not gw.is_unspecified:
                        out.add(gw)
            except ValueError:
                continue
    return out


@_cached
def _eth0_is_veth():
    """True on a Docker bridge (eth0 is one end of a veth pair), False on
    macvlan/ipvlan or a real NIC, None when we cannot tell."""
    try:
        uevent = open(_ETH0_UEVENT).read()
    except OSError:
        return None
    # The kernel names a veth end DEVTYPE=veth. macvlan, ipvlan and real
    # NICs are something else.
    return "DEVTYPE=veth" in uevent


def _nets_of(adapters, skip_lo=True) -> list:
    out = []
    for nic in adapters:
        for ip in nic.ips:
            raw = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            try:
                addr = ipaddress.ip_address(raw)
                if skip_lo and addr.is_loopback:
                    continue
                out.append((nic, ipaddress.ip_network(
                    f"{raw}/{ip.network_prefix}", strict=False)))
            except ValueError:
                continue
    return out


def _docker_nic(nic) -> bool:
    return str(nic.nice_name).startswith(announce._DOCKER_NICS)


@_cached
def _never_home() -> tuple:
    """(addresses, networks) that are never home, from Docker's own wiring."""
    addrs, nets = set(), []
    try:
        pairs = _nets_of(list(_adapters()))
    except Exception:
        pairs = []
    if paths._in_container() and not announce._host_network():
        addrs |= _gateways()
        # On a bridge, our own subnet is Docker's. Unknown driver: strict.
        if _eth0_is_veth() is not False:
            nets += [net for _, net in pairs]
    else:
        nets += [net for nic, net in pairs if _docker_nic(nic)]
    return addrs, nets


def _norm(addr):
    """IPv4-mapped IPv6 is its IPv4."""
    if addr.version == 6 and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _parse(text):
    try:
        return _norm(ipaddress.ip_address(str(text).strip()))
    except ValueError:
        return None


@_cached
def local_subnets() -> list:
    """The networks this machine is on, from its own interfaces. Docker's
    own networks are left out, as announce does."""
    try:
        adapters = list(_adapters())
    except Exception:
        return []
    out = []
    for nic in adapters:
        if str(nic.nice_name).startswith(announce._DOCKER_NICS):
            continue
        for ip in nic.ips:
            raw = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            if announce.is_bridge(raw):
                continue
            try:
                net = ipaddress.ip_network(f"{raw}/{ip.network_prefix}", strict=False)
            except ValueError:
                continue
            if net.prefixlen < (16 if net.version == 4 else 48):
                continue                # too wide to mean "my home"
            if net not in out:
                out.append(net)
    return out


def _trusted():
    """Setting trusted_proxies: comma-separated addresses or CIDRs."""
    out = []
    for part in (db.get_setting("trusted_proxies") or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            log.warning("trusted_proxies: ignoring %r", part)
            continue
        if net.version == 6 and net.network_address.ipv4_mapped and net.prefixlen >= 96:
            net = ipaddress.ip_network(
                (net.network_address.ipv4_mapped, net.prefixlen - 96))
        out.append(net)
    return out


def _is_trusted(addr, trusted) -> bool:
    for net in trusted:
        if addr.version == net.version and addr in net:
            return True
    return False


# Not a real address, so it is never home.
_NOBODY = ipaddress.ip_address("0.0.0.0")


def client_address(request):
    """The real client. The direct peer, unless it is a trusted proxy, then
    the right-most X-Forwarded-For entry that is not a trusted proxy."""
    peer = _parse(getattr(getattr(request, "client", None), "host", None))
    if peer is None:
        return _NOBODY
    trusted = _trusted()
    if not trusted or not _is_trusted(peer, trusted):
        return peer
    lines = request.headers.getlist("x-forwarded-for")
    if not lines:
        return _NOBODY              # a proxy that says nothing: outside
    hops = []
    for part in ",".join(lines).split(","):
        addr = _parse(part)
        if addr is None:
            return _NOBODY          # junk: treat as outside
        hops.append(addr)
    for addr in reversed(hops):
        if not _is_trusted(addr, trusted):
            return addr
    return hops[0]


def is_home(request) -> bool:
    addr = client_address(request)
    if addr == _NOBODY:
        return False
    addrs, nets_never = _never_home()
    if addr in addrs or any(addr.version == n.version and addr in n for n in nets_never):
        return False
    nets = _HOME + local_subnets()
    return any(addr.version == n.version and addr in n for n in nets)


def away_on() -> bool:
    return db.get_setting("away_mode") == "1"
