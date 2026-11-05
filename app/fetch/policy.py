"""URL allow/block rules and SSRF checks for the fetch gateway."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

Resolver = Callable[[str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]]

BLOCKED_SUFFIXES = ("linkedin.com", "indeed.com", "glassdoor.com")

DEFAULT_API_ALLOWLIST = frozenset(
    {
        "api.greenhouse.io",
        "boards-api.greenhouse.io",
        "api.lever.co",
        "eu.lever.co",
        "www.reed.co.uk",
        "api.reed.co.uk",
        "api.adzuna.com",
        "api.adzuna.co.uk",
    }
)

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


@dataclass(frozen=True, slots=True)
class PolicyRefusal:
    reason: str


def default_resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    return addresses


def host_matches_suffix(host: str, suffix: str) -> bool:
    host = host.lower().rstrip(".")
    suffix = suffix.lower().rstrip(".")
    return host == suffix or host.endswith("." + suffix)


def is_blocked_host(host: str) -> bool:
    return any(host_matches_suffix(host, suffix) for suffix in BLOCKED_SUFFIXES)


def is_allowlisted_host(host: str, allowlist: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host_matches_suffix(host, allowed) for allowed in allowlist)


def is_official_domain_host(host: str, official_domain: str) -> bool:
    return host_matches_suffix(host, official_domain)


def is_disallowed_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_multicast or address.is_unspecified:
        return True
    return any(address in network for network in _PRIVATE_NETWORKS)


def check_url(
    url: str,
    *,
    kind: str,
    official_domain: str | None,
    allowlist: frozenset[str],
    resolve: Resolver = default_resolve,
) -> PolicyRefusal | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return PolicyRefusal("scheme")
    if parsed.username is not None or parsed.password is not None:
        return PolicyRefusal("userinfo")
    host = parsed.hostname
    if not host:
        return PolicyRefusal("scheme")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return PolicyRefusal("private_address")
    if is_blocked_host(host):
        return PolicyRefusal("blocked_host")
    if kind == "api":
        if not is_allowlisted_host(host, allowlist):
            return PolicyRefusal("allowlist")
    elif kind == "page":
        if not official_domain or not is_official_domain_host(host, official_domain):
            return PolicyRefusal("allowlist")
    else:
        return PolicyRefusal("allowlist")
    try:
        addresses = list(resolve(host))
    except OSError:
        return PolicyRefusal("private_address")
    if not addresses:
        return PolicyRefusal("private_address")
    if any(is_disallowed_ip(address) for address in addresses):
        return PolicyRefusal("private_address")
    return None
