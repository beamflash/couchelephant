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
    _forwarded.clear()


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


def _eth0_driver():
    """The kernel driver behind eth0 ("veth", "macvlan", "ipvlan", ...), or
    None. Asked through the ethtool ioctl: /sys uevent carries no DEVTYPE for
    any of these, checked on real Docker networks."""
    import array
    import fcntl
    import struct
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            buf = array.array("B", struct.pack("I", 3) + b"\0" * 192)  # ETHTOOL_GDRVINFO
            ifr = struct.pack("16sP", b"eth0", buf.buffer_info()[0])
            fcntl.ioctl(s.fileno(), 0x8946, ifr)                     # SIOCETHTOOL
        return bytes(buf[4:36]).split(b"\0")[0].decode() or None
    except (OSError, ImportError, ValueError):
        return None


@_cached
def _eth0_is_veth():
    """True on a Docker bridge, False on macvlan or ipvlan (the container is
    on the LAN itself), None when we cannot tell, which callers treat as a
    bridge."""
    drv = _eth0_driver()
    if drv == "veth":
        return True
    if drv in ("macvlan", "ipvlan"):
        return False
    return None


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


# A trusted proxy is one address or a small block, never "everyone".
MIN_PREFIX = {4: 8, 6: 32}


def _too_wide(net) -> bool:
    return net.prefixlen < MIN_PREFIX[net.version]


def parse_trusted(text):
    """(networks, error) for a comma-separated list. Any bad or too wide entry
    refuses the whole list, so nothing half-saved is trusted."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            return [], f"{part} is not an address or a block like 10.0.0.0/8."
        if net.version == 6 and net.network_address.ipv4_mapped and net.prefixlen >= 96:
            net = ipaddress.ip_network(
                (net.network_address.ipv4_mapped, net.prefixlen - 96))
        if _too_wide(net):
            return [], (f"{part} is too wide. Name the proxy's own address, or a "
                        f"block no wider than /{MIN_PREFIX[net.version]}.")
        out.append(net)
    return out, ""


def _trusted():
    """Setting trusted_proxies: comma-separated addresses or CIDRs. An entry
    that is bad or too wide is skipped, whatever put it there."""
    out = []
    for part in (db.get_setting("trusted_proxies") or "").split(","):
        if not part.strip():
            continue
        nets, error = parse_trusted(part)
        if error:
            log.warning("trusted_proxies: ignoring %r", part.strip())
            continue
        out += nets
    return out


def _is_trusted(addr, trusted) -> bool:
    for net in trusted:
        if addr.version == net.version and addr in net:
            return True
    return False


# Not a real address, so it is never home.
_NOBODY = ipaddress.ip_address("0.0.0.0")


# The last proxy we saw that nobody told us to trust: (peer, time). The
# self-check reads it.
_forwarded: list = []
FORWARD_MEMORY = 24 * 3600


def untrusted_forward():
    """The peer of a recent request that carried a forward header but was not
    from a trusted proxy, or None."""
    if _forwarded and time.time() - _forwarded[1] < FORWARD_MEMORY:
        return _forwarded[0]
    return None


def clear_forward() -> None:
    _forwarded.clear()


def client_address(request):
    """The real client. The direct peer, unless it is a trusted proxy, then
    the right-most X-Forwarded-For entry that is not a trusted proxy. A
    forward header from any other peer means a proxy we were not told about:
    every visitor looks like it, so none is home (fail safe)."""
    peer = _parse(getattr(getattr(request, "client", None), "host", None))
    if peer is None:
        return _NOBODY
    trusted = _trusted()
    if not trusted or not _is_trusted(peer, trusted):
        if any(request.headers.get(h) is not None
               for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
            _forwarded[:] = [str(peer), time.time()]
            return _NOBODY
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


def peer_is_home(host) -> bool:
    """For a socket's peer address, before any request: the same rules as
    is_home, and a trusted proxy counts as home."""
    addr = _parse(host)
    if addr is None:
        return False
    if _is_trusted(addr, _trusted()):
        return True
    return _addr_is_home(addr)


def peer_is_bridge(host) -> bool:
    """A Docker gateway or bridge address: every visitor of a bridge
    container arrives from it, so it is never limited as one person."""
    addr = _parse(host)
    if addr is None:
        return False
    addrs, nets_never = _never_home()
    return addr in addrs or any(addr.version == n.version and addr in n for n in nets_never)


def is_home(request) -> bool:
    return _addr_is_home(client_address(request))


def _addr_is_home(addr) -> bool:
    if addr == _NOBODY:
        return False
    addrs, nets_never = _never_home()
    if addr in addrs or any(addr.version == n.version and addr in n for n in nets_never):
        return False
    nets = _HOME + local_subnets()
    return any(addr.version == n.version and addr in n for n in nets)


def away_on() -> bool:
    return db.get_setting("away_mode") == "1"
