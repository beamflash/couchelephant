"""Who is at home. The away gate, HTTPS and app pinning all ask this.

Home means loopback, RFC 1918, link-local, IPv6 ULA, and the subnets this
machine sits on. Everything else is outside, including the router's own
public address (hairpin NAT)."""
import ipaddress
import logging

from . import announce, db

log = logging.getLogger("couchelephant.exposure")

_HOME = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


def _adapters():
    import ifaddr
    return ifaddr.get_adapters()


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
        out.append(net)
    return out


def _is_trusted(addr, trusted) -> bool:
    for net in trusted:
        if net.version == 6 and net.network_address.ipv4_mapped:
            continue
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
    header = request.headers.get("x-forwarded-for")
    if header is None:
        return peer
    hops = []
    for part in header.split(","):
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
    nets = _HOME + local_subnets()
    return any(addr.version == n.version and addr in n for n in nets)


def away_on() -> bool:
    return db.get_setting("away_mode") == "1"
