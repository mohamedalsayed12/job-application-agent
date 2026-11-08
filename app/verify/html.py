"""Tiny HTML helpers for official-domain and ownership checks."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_DESCRIPTION_NAME_RE = re.compile(r"""(?:name|property)=["'](?:og:)?description["']""", re.IGNORECASE)
_CONTENT_RE = re.compile(r"""content=(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def page_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if match is None:
        return None
    text = _TAG_RE.sub(" ", match.group(1))
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def meta_description(html: str) -> str:
    for match in _META_RE.finditer(html):
        tag = match.group(0)
        if _DESCRIPTION_NAME_RE.search(tag):
            content = _CONTENT_RE.search(tag)
            if content:
                return unescape(content.group(1) or content.group(2) or "").strip()
    return ""


def extract_hrefs(html: str, *, base_url: str) -> tuple[str, ...]:
    return _extract_urls(html, base_url=base_url, tag="a", attribute="href")


def extract_script_srcs(html: str, *, base_url: str) -> tuple[str, ...]:
    return _extract_urls(html, base_url=base_url, tag="script", attribute="src")


def _extract_urls(html: str, *, base_url: str, tag: str, attribute: str) -> tuple[str, ...]:
    parser = _UrlParser(base_url, tag=tag, attribute=attribute)
    try:
        parser.feed(html)
    except Exception:
        return tuple(parser.urls)
    return tuple(parser.urls)


class _UrlParser(HTMLParser):
    def __init__(self, base_url: str, *, tag: str, attribute: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._tag = tag
        self._attribute = attribute
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != self._tag:
            return
        for name, value in attrs:
            if name.lower() == self._attribute and value:
                self.urls.append(urljoin(self._base, value.strip()))
                return


def host_of(url: str) -> str | None:
    host = urlparse(url).hostname
    if host is None:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host
