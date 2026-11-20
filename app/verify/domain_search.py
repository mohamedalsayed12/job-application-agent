"""Find and verify a company's official website domain (D55)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.fetch.policy import BLOCKED_SUFFIXES, PUBLIC_SUFFIX_BLOCKLIST, host_matches_suffix

DDG_HTML = "https://html.duckduckgo.com/html/"
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
_JOB_BOARD_HOSTS = BLOCKED_SUFFIXES + ("duckduckgo.com", "google.com", "bing.com", "yahoo.com")


@dataclass(frozen=True, slots=True)
class DomainResult:
    domain: str | None
    reason: str
    candidates: tuple[str, ...]


def apply_host_domain(apply_url: str | None) -> str | None:
    """Use the apply link's registrable host when it is not a job board."""
    if not apply_url:
        return None
    host = urlparse(apply_url).hostname
    if not host:
        return None
    host = host.lower().rstrip(".")
    if any(host_matches_suffix(host, blocked) for blocked in _JOB_BOARD_HOSTS):
        return None
    return registrable_domain(host)


# Multi-part public suffixes beyond the short list in fetch.policy.
_MULTI_PART_SUFFIXES = frozenset(PUBLIC_SUFFIX_BLOCKLIST) | frozenset(
    {
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "org.nz",
        "co.jp",
        "com.br",
        "co.in",
        "com.sg",
    }
)


def registrable_domain(host: str) -> str | None:
    host = host.lower().rstrip(".")
    if "." not in host or host in _MULTI_PART_SUFFIXES:
        return None
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        domain = ".".join(labels[-3:])
    else:
        domain = ".".join(labels[-2:])
    if domain in _MULTI_PART_SUFFIXES or "." not in domain:
        return None
    return domain


def _looks_like_company(domain: str, company_name: str) -> bool:
    slug = re.sub(r"[^a-z0-9]+", "", company_name.casefold())
    if len(slug) < 3:
        return False
    base = domain.split(".")[0].replace("-", "")
    if len(base) < 3:
        return False
    return slug[: min(6, len(slug))] in base or base[: min(6, len(base))] in slug


def parse_ddg_result_urls(html: str) -> list[str]:
    urls: list[str] = []
    for match in _HREF.finditer(html):
        href = match.group(1)
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            target = qs.get("uddg", [None])[0]
            if target:
                urls.append(unquote(target))
            continue
        if href.startswith("http"):
            urls.append(href)
    return urls


async def find_official_domain(
    gateway: FetchGateway,
    company_name: str,
    *,
    apply_url: str | None,
    known_title: str | None = None,
) -> DomainResult:
    from_apply = apply_host_domain(apply_url)
    if from_apply and await _homepage_mentions(gateway, from_apply, company_name, known_title):
        return DomainResult(from_apply, "apply_host_verified", (from_apply,))

    query = f'"{company_name}" official website'
    outcome = await gateway.fetch(
        FetchRequest(
            url=f"{DDG_HTML}?q={_quote(query)}",
            kind="page",
            official_domain="duckduckgo.com",
            timeout_s=15.0,
        )
    )
    if isinstance(outcome, FetchRefusal):
        return DomainResult(from_apply, f"domain_search_fetch:{outcome.reason}", ())
    if not isinstance(outcome, FetchResponse) or outcome.status_code != 200:
        return DomainResult(from_apply, "domain_search_http_error", ())

    try:
        html = outcome.body.decode("utf-8", errors="replace")
    except Exception:
        return DomainResult(from_apply, "domain_search_bad_body", ())

    candidates: list[str] = []
    for url in parse_ddg_result_urls(html):
        host = urlparse(url).hostname
        if not host:
            continue
        domain = registrable_domain(host)
        if domain is None or any(host_matches_suffix(domain, blocked) for blocked in _JOB_BOARD_HOSTS):
            continue
        if not _looks_like_company(domain, company_name):
            continue
        if domain not in candidates:
            candidates.append(domain)
        if len(candidates) >= 5:
            break

    for domain in candidates:
        if await _homepage_mentions(gateway, domain, company_name, known_title):
            return DomainResult(domain, "search_verified", tuple(candidates))

    # Never treat an unverified apply host as official (soft gate / phishing).
    return DomainResult(None, "domain_not_found", tuple(candidates))


async def _homepage_mentions(
    gateway: FetchGateway,
    domain: str,
    company_name: str,
    known_title: str | None,
) -> bool:
    outcome = await gateway.fetch(
        FetchRequest(url=f"https://{domain}/", kind="page", official_domain=domain, timeout_s=15.0)
    )
    if isinstance(outcome, FetchRefusal) or not isinstance(outcome, FetchResponse):
        return False
    if outcome.status_code >= 400:
        return False
    text = _visible_text(outcome.body.decode("utf-8", errors="replace")).casefold()
    needles = [company_name.casefold()]
    if known_title:
        needles.append(known_title.casefold())
    return any(needle and needle in text for needle in needles)


def _quote(query: str) -> str:
    from urllib.parse import quote

    return quote(query, safe="")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(self._chunks)


def _visible_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()
