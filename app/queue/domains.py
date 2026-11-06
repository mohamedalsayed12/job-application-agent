"""Normalize registrable hostnames for candidate domain matching."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_DOMAIN_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}",
    re.IGNORECASE,
)


def normalize_domain(value: str | None) -> str | None:
    """Lowercase host; strip scheme, path, trailing dot, and leading www."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "://" in text:
        host = urlparse(text).hostname
        if host is None:
            return None
        text = host
    text = text.rstrip(".").split("/")[0]
    if text.startswith("www."):
        text = text[4:]
    if not _DOMAIN_RE.fullmatch(text):
        return None
    return text
