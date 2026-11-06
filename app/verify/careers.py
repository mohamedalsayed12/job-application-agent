"""Detect MVP-unsupported careers systems on an official domain (T17)."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.models import VerificationOutcome
from app.verify.html import extract_hrefs, host_of
from app.verify.types import (
    UNSUPPORTED_CAREERS_MARKERS,
    CheckRecord,
    UnsupportedCareers,
)

_CAREERS_PATHS = ("/careers", "/careers/", "/jobs", "/jobs/", "/about/careers")
_RETRYABLE = frozenset({"timeout", "http_error"})


def detect_unsupported_marker(url: str, html: str = "") -> str | None:
    haystack = f"{url}\n{html}".lower()
    for system, marker in UNSUPPORTED_CAREERS_MARKERS:
        if marker in haystack:
            return system
    return None


async def find_unsupported_careers(
    gateway: FetchGateway,
    *,
    domain: str,
    homepage_html: str | None = None,
    homepage_url: str | None = None,
) -> UnsupportedCareers | None | str:
    """Return UnsupportedCareers, None if none found, or 'retry' on transport error."""
    pages: list[tuple[str, str]] = []
    if homepage_html is not None and homepage_url is not None:
        pages.append((homepage_url, homepage_html))
        for href in extract_hrefs(homepage_html, base_url=homepage_url):
            if _looks_like_careers_link(href, domain):
                fetched = await _fetch_page(gateway, href, domain)
                if fetched == "retry":
                    return "retry"
                if fetched is not None:
                    pages.append(fetched)

    for path in _CAREERS_PATHS:
        url = f"https://{domain}{path}"
        if any(p[0].rstrip("/") == url.rstrip("/") for p in pages):
            continue
        fetched = await _fetch_page(gateway, url, domain)
        if fetched == "retry":
            return "retry"
        if fetched is not None:
            pages.append(fetched)

    for url, html in pages:
        system = detect_unsupported_marker(url, html)
        if system is not None:
            snippet = _snippet_for(system, url, html)
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
        for href in extract_hrefs(html, base_url=url):
            system = detect_unsupported_marker(href)
            if system is not None:
                snippet = href[:200]
                return UnsupportedCareers(
                    system=system,
                    evidence_url=href,
                    evidence_snippet=snippet,
                    record=CheckRecord(
                        check_type="unsupported_careers",
                        outcome=VerificationOutcome.pass_,
                        evidence_url=href,
                        evidence_snippet=snippet,
                    ),
                )
    return None


def _looks_like_careers_link(url: str, domain: str) -> bool:
    host = host_of(url)
    if host is None:
        return False
    if host == domain or host.endswith("." + domain):
        path = (urlparse(url).path or "").lower()
        return any(token in path for token in ("career", "job", "join", "vacanc"))
    # Off-domain Workday/Ashby hosts linked from the official site.
    return detect_unsupported_marker(url) is not None


async def _fetch_page(
    gateway: FetchGateway,
    url: str,
    domain: str,
) -> tuple[str, str] | None | str:
    outcome = await gateway.fetch(
        FetchRequest(url=url, kind="page", official_domain=domain)
    )
    if isinstance(outcome, FetchRefusal):
        if outcome.reason in _RETRYABLE:
            return "retry"
        # Cross-host Workday links are not under official_domain; record URL only.
        if outcome.reason == "allowlist" and detect_unsupported_marker(url):
            return (url, "")
        return None
    assert isinstance(outcome, FetchResponse)
    if outcome.status_code != 200:
        return None
    return (outcome.url, outcome.body.decode("utf-8", errors="replace"))


def _snippet_for(system: str, url: str, html: str) -> str:
    for _, marker in UNSUPPORTED_CAREERS_MARKERS:
        if marker in url.lower() or marker in html.lower():
            return marker[:200]
    return f"{system}:{url}"[:200]


def careers_probe_url(domain: str, path: str = "/careers") -> str:
    return urljoin(f"https://{domain}/", path.lstrip("/"))
