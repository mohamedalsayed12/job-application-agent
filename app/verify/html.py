"""Tiny HTML helpers for official-domain and ownership checks."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def page_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if match is None:
        return None
    text = _TAG_RE.sub(" ", match.group(1))
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def extract_hrefs(html: str, *, base_url: str) -> tuple[str, ...]:
    parser = _HrefParser(base_url)
    try:
        parser.feed(html)
    except Exception:
        return tuple(parser.hrefs)
    return tuple(parser.hrefs)


class _HrefParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(urljoin(self._base, value.strip()))
                return


def host_of(url: str) -> str | None:
    host = urlparse(url).hostname
    if host is None:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host
