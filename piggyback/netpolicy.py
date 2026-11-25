"""Which hosts and schemes a reference fetch may touch. Shared by every network read.

Loopback is allowed for one pinned origin only: loopback is the (address, port) that origin()
gives for the approved URL, or None. Public destinations keep the same policy with or without it.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443}
BLOCKED_NAMES = ("localhost", ".localhost", ".local")


def _unwrapped(ip):
    # Older 3.11 patch releases judge ::ffff:a.b.c.d by its IPv6 form and call 6to4 and Teredo
    # global, so unwrap the mapped form and refuse the tunnels (they can wrap any IPv4): None.
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None or ip.teredo is not None:
            return None
    return ip


def ip_allowed(ip, loopback=None, port=None):
    ip = _unwrapped(ip)
    if ip is None:
        return False
    if ip.is_loopback:
        return loopback is not None and loopback == (str(ip), port)
    # is_global rejects CGNAT, private and unspecified space; not multicast, reserved space such
    # as 4000::/3 or deprecated IPv6 site-local fec0::/10, so those stay explicit.
    site_local = ip.version == 6 and ip.is_site_local
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or site_local)


def _addresses(name):
    """Every address a host (a literal or a name) resolves to, or None if it does not resolve."""
    try:
        try:
            return [ipaddress.ip_address(name)]
        except ValueError:
            infos = socket.getaddrinfo(name, None)
            return [ipaddress.ip_address(str(info[4][0])) for info in infos]
    except (OSError, ValueError):
        return None


def origin(url):
    """(address, port) of an approved URL: the loopback origin it pins, or None.

    A name pins the address it resolves to, so a local build given by name is reached at that
    address and port only. A name that does not resolve pins nothing it can match.
    """
    try:
        parts = urlsplit(url)
        port = parts.port or DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    name = (parts.hostname or "").lower().rstrip(".")
    if not name or port is None:
        return None
    addresses = _addresses(name)
    if not addresses:
        return (name, port)
    ip = addresses[0]
    if ip.version == 6:
        ip = ip.ipv4_mapped or ip
    return (str(ip), port)


def resolve_allowed(host, port=None, loopback=None):
    """Return one address to connect to, or None. Every address the name resolves to must pass.

    Callers connect to the returned literal and never resolve the name again: that closes DNS
    rebinding for the connection.
    """
    name = host.lower().rstrip(".")
    if not name or name.endswith(BLOCKED_NAMES):  # BLOCKED_NAMES includes bare "localhost"
        return None
    addresses = _addresses(name)
    if not addresses or not all(ip_allowed(ip, loopback, port) for ip in addresses):
        return None
    return str(addresses[0])


def several_loopback(url):
    """True when the URL's name resolves to two or more addresses, all loopback: one pin is one."""
    try:
        name = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    addresses = [_unwrapped(ip) for ip in _addresses(name) or []]
    return len(addresses) > 1 and all(ip is not None and ip.is_loopback for ip in addresses)


def host_allowed(host, port=None, loopback=None):
    return resolve_allowed(host, port, loopback) is not None


def url_allowed(url, loopback=None):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in SCHEMES:
        return False
    return host_allowed(parts.hostname or "", port or DEFAULT_PORTS[parts.scheme], loopback)


def local_name(url):
    """`localhost` means 127.0.0.1 (same port and path). The policy refuses the name itself."""
    try:
        parts = urlsplit(url)
        if parts.hostname != "localhost":
            return url
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return url
    return parts._replace(netloc=f"127.0.0.1{port}").geturl()
