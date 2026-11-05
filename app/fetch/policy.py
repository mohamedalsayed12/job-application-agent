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
    # is_global is False for private, loopback, link-local, CGNAT, ULA, unspecified.
    # Multicast reports is_global on some builds, so keep it explicit.
    return address.is_multicast or not address.is_global


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
    assert host is not None  # check_ssrf already rejected missing hosts
    if kind == "api":
        if not is_allowlisted_host(host, allowlist):
            return "allowlist"
    elif not official_domain or not is_official_domain_host(host, official_domain):
        return "allowlist"
    return None
