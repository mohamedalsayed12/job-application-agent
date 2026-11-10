"""URL allow/block rules and SSRF checks for the fetch gateway."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Literal
from urllib.parse import urlparse

Resolver = Callable[[str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]]
PolicyReason = Literal[
    "blocked_host",
    "private_address",
    "allowlist",
    "scheme",
    "userinfo",
]

BLOCKED_SUFFIXES = ("linkedin.com", "indeed.com", "glassdoor.com")
# Reject bare public suffixes until a real PSL check exists (P2 / thermos).
PUBLIC_SUFFIX_BLOCKLIST = frozenset(
    {
        "com",
        "org",
        "net",
        "io",
        "co",
        "uk",
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
    }
)
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


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
    domain = official_domain.lower().rstrip(".")
    if "." not in domain or domain in PUBLIC_SUFFIX_BLOCKLIST:
        return False
    return host_matches_suffix(host, domain)


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_disallowed_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_multicast:
        return True
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return not embedded.is_global
    return not address.is_global


def check_ssrf(url: str, *, resolve: Resolver = default_resolve) -> PolicyReason | None:
    """HTTPS / no userinfo / named host / not blocked / no private DNS — no allowlist."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "scheme"
    if parsed.username is not None or parsed.password is not None:
        return "userinfo"
    host = parsed.hostname
    if not host:
        return "scheme"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "private_address"
    if is_blocked_host(host):
        return "blocked_host"
    try:
        addresses = list(resolve(host))
    except OSError:
        return "private_address"
    if not addresses:
        return "private_address"
    if any(is_disallowed_ip(address) for address in addresses):
        return "private_address"
    return None


def check_url(
    url: str,
    *,
    kind: Literal["api", "page"],
    official_domain: str | None,
    allowlist: frozenset[str],
    resolve: Resolver = default_resolve,
) -> PolicyReason | None:
    refusal = check_ssrf(url, resolve=resolve)
    if refusal is not None:
        return refusal
    host = urlparse(url).hostname
    assert host is not None
    if kind == "api":
        if not is_allowlisted_host(host, allowlist):
            return "allowlist"
    elif not official_domain or not is_official_domain_host(host, official_domain):
        return "allowlist"
    return None
