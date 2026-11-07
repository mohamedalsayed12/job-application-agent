"""Detect MVP-unsupported careers systems on an official domain (T17)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from app.fetch.gateway import FetchGateway
from app.fetch.policy import host_matches_suffix
from app.models import VerificationOutcome
from app.verify.html import extract_hrefs, host_of
from app.verify.pages import fetch_official_page
from app.verify.types import (
    UNSUPPORTED_CAREERS_MARKERS,
    CheckRecord,
    UnsupportedCareers,
)

_CAREERS_PATHS = ("/careers", "/jobs")
_MAX_PAGE_FETCHES = 3

CareersResult = UnsupportedCareers | None | Literal["retry"]


def detect_unsupported_host(url: str) -> str | None:
    """Match unsupported ATS by link host suffix only (never page-body substrings)."""
    host = host_of(url)
    if host is None:
        return None
    for system, marker in UNSUPPORTED_CAREERS_MARKERS:
        if host_matches_suffix(host, marker):
            return system
    return None


async def find_unsupported_careers(
    gateway: FetchGateway,
    *,
    domain: str,
    homepage_html: str | None = None,
    homepage_url: str | None = None,
) -> CareersResult:
    """Return UnsupportedCareers, None if none found, or ``\"retry\"`` on transport error."""
    pages: list[tuple[str, str]] = []
    fetches = 0

    if homepage_html is not None and homepage_url is not None:
        pages.append((homepage_url, homepage_html))
        for href in extract_hrefs(homepage_html, base_url=homepage_url):
            system = detect_unsupported_host(href)
            if system is not None:
                return _hit(system, href)
            if fetches >= _MAX_PAGE_FETCHES:
                break
            if not _looks_like_same_domain_careers(href, domain):
                continue
            fetched = await fetch_official_page(gateway, href, domain=domain)
            fetches += 1
            if fetched.kind == "retry":
                return "retry"
            if fetched.kind == "ok" and fetched.url and fetched.html is not None:
                pages.append((fetched.url, fetched.html))

    for path in _CAREERS_PATHS:
        if fetches >= _MAX_PAGE_FETCHES:
            break
        url = f"https://{domain}{path}"
        if any(p[0].rstrip("/") == url.rstrip("/") for p in pages):
            continue
        fetched = await fetch_official_page(gateway, url, domain=domain)
        fetches += 1
        if fetched.kind == "retry":
            return "retry"
        if fetched.kind == "ok" and fetched.url and fetched.html is not None:
            pages.append((fetched.url, fetched.html))

    for url, html in pages:
        # Current page URL itself may be a Workday vanity host under official_domain
        # only when the host matches — normally Workday is off-domain via href.
        system = detect_unsupported_host(url)
        if system is not None:
            return _hit(system, url)
        for href in extract_hrefs(html, base_url=url):
            system = detect_unsupported_host(href)
            if system is not None:
                return _hit(system, href)
    return None


def _hit(system: str, url: str) -> UnsupportedCareers:
    snippet = (host_of(url) or url)[:200]
    return UnsupportedCareers(
        system=system,
        evidence_url=url,
        evidence_snippet=snippet,
        record=CheckRecord(
            check_type="unsupported_careers",
            outcome=VerificationOutcome.pass_,
            evidence_url=url,
            evidence_snippet=snippet,
        ),
    )


def _looks_like_same_domain_careers(url: str, domain: str) -> bool:
    host = host_of(url)
    if host is None:
        return False
    if not (host == domain or host.endswith("." + domain)):
        return False
    path = (urlparse(url).path or "").lower()
    return any(token in path for token in ("career", "job", "join", "vacanc"))
