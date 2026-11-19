"""Check whether a job title appears on the company's careers pages (D55)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.cleaning.linking import title_key
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.verify.domain_search import _visible_text

CAREERS_PATHS = ("/careers", "/jobs", "/vacancies", "/join-us", "/work-with-us", "/careers/")
_CAREERS_HREF = re.compile(
    r"""href=["']([^"']*(?:career|job|vacanc|join[-_]?us|work[-_]?with[-_]?us)[^"']*)["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ListingHit:
    found: bool
    apply_url: str | None
    reason: str
    pages_checked: tuple[str, ...]


async def find_listing_on_site(
    gateway: FetchGateway,
    *,
    domain: str,
    title: str,
) -> ListingHit:
    key = title_key(title)
    if not key:
        return ListingHit(False, None, "empty_title", ())

    seeds = [f"https://{domain}{path}" for path in CAREERS_PATHS]
    home = await _fetch_page(gateway, f"https://{domain}/", domain)
    pages_checked: list[str] = []
    if home is not None:
        pages_checked.append(f"https://{domain}/")
        hit = _title_on_page(home, key, f"https://{domain}/")
        if hit:
            return ListingHit(True, hit, "title_on_homepage", tuple(pages_checked))
        for href in _CAREERS_HREF.findall(home):
            absolute = urljoin(f"https://{domain}/", href)
            if urlparse(absolute).hostname and domain in (urlparse(absolute).hostname or ""):
                if absolute not in seeds:
                    seeds.append(absolute)

    for url in seeds[:8]:
        html = await _fetch_page(gateway, url, domain)
        if html is None:
            continue
        pages_checked.append(url)
        hit = _title_on_page(html, key, url)
        if hit:
            return ListingHit(True, hit, "title_on_careers_page", tuple(pages_checked))

    return ListingHit(False, None, "listing_not_on_site", tuple(pages_checked))


async def _fetch_page(gateway: FetchGateway, url: str, domain: str) -> str | None:
    outcome = await gateway.fetch(
        FetchRequest(url=url, kind="page", official_domain=domain, timeout_s=15.0)
    )
    if isinstance(outcome, FetchRefusal) or not isinstance(outcome, FetchResponse):
        return None
    if outcome.status_code >= 400:
        return None
    return outcome.body.decode("utf-8", errors="replace")


def _title_on_page(html: str, key: str, page_url: str) -> str | None:
    text = title_key(_visible_text(html))
    if key in text:
        return _best_apply_url(html, key, page_url) or page_url
    # Also accept the raw HTML (titles in attributes / JSON-LD).
    if key in title_key(re.sub(r"<[^>]+>", " ", html)):
        return _best_apply_url(html, key, page_url) or page_url
    return None


def _best_apply_url(html: str, key: str, page_url: str) -> str | None:
    """Prefer an <a> whose text contains the job title. Empty or tiny labels never win."""
    page_host = (urlparse(page_url).hostname or "").lower()

    class _Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._capture: str | None = None
            self._buf: list[str] = []
            self.found: str | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "a":
                return
            self._capture = dict(attrs).get("href")
            self._buf = []

        def handle_endtag(self, tag: str) -> None:
            if tag != "a" or self._capture is None:
                return
            label = title_key("".join(self._buf))
            absolute = urljoin(page_url, self._capture)
            parsed = urlparse(absolute)
            host = (parsed.hostname or "").lower()
            same_site = host == page_host or (page_host and host.endswith("." + page_host))
            if (
                len(label) >= 4
                and key in label
                and parsed.scheme in {"http", "https"}
                and same_site
                and self.found is None
            ):
                self.found = absolute
            self._capture = None

        def handle_data(self, data: str) -> None:
            if self._capture is not None:
                self._buf.append(data)

    parser = _Links()
    try:
        parser.feed(html)
    except Exception:
        return None
    return parser.found
