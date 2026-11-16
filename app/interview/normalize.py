"""Turn Apify platform items into short evidence rows (url, title, snippet)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.models import InterviewPlatform

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class NormalizedItem:
    platform: InterviewPlatform
    url: str
    title: str
    snippet: str
    content_hash: str
    source_id: str


def queries_for(role: str) -> list[str]:
    base = role.strip()
    return [
        f"{base} interview got the offer",
        f"{base} interview rejected",
        f"{base} interview preparation tips",
    ]


def queries_for_company(company: str, role: str) -> list[str]:
    company = company.strip()
    role = role.strip()
    return [f'"{company}" {role} interview', f'"{company}" interview experience']


def normalize_item(
    platform: InterviewPlatform,
    item: dict[str, Any],
    *,
    max_snippet: int,
) -> NormalizedItem | None:
    if platform is InterviewPlatform.reddit:
        url, title, body, source_id = _reddit(item)
    elif platform is InterviewPlatform.youtube:
        url, title, body, source_id = _youtube(item)
    else:
        url, title, body, source_id = _linkedin(item)
    if not url or not _public_http(url):
        return None
    title = _clean(title) or url
    snippet = _clean(body)[:max_snippet]
    if not snippet:
        return None
    digest = hashlib.sha256(f"{platform.value}\n{url}\n{snippet}".encode()).hexdigest()
    return NormalizedItem(
        platform=platform,
        url=url,
        title=title[:300],
        snippet=snippet,
        content_hash=digest,
        source_id=source_id or digest[:32],
    )


def _reddit(item: dict[str, Any]) -> tuple[str, str, str, str]:
    url = str(item.get("url") or item.get("parsedId") or item.get("link") or "").strip()
    if url and url.startswith("/"):
        url = f"https://www.reddit.com{url}"
    title = str(item.get("title") or "")
    body = str(item.get("body") or item.get("text") or item.get("selftext") or item.get("data") or "")
    if isinstance(item.get("data"), dict):
        data = item["data"]
        url = str(data.get("url") or data.get("permalink") or url).strip()
        if url.startswith("/"):
            url = f"https://www.reddit.com{url}"
        title = str(data.get("title") or title)
        body = str(data.get("selftext") or data.get("body") or body)
    source_id = str(item.get("id") or item.get("parsedId") or url)
    return url, title, f"{title}\n{body}".strip(), source_id


def _youtube(item: dict[str, Any]) -> tuple[str, str, str, str]:
    url = str(item.get("url") or item.get("videoUrl") or "").strip()
    if not url and item.get("id"):
        url = f"https://www.youtube.com/watch?v={item['id']}"
    title = str(item.get("title") or item.get("name") or "")
    body = str(
        item.get("text")
        or item.get("description")
        or item.get("transcript")
        or item.get("caption")
        or ""
    )
    source_id = str(item.get("id") or url)
    return url, title, f"{title}\n{body}".strip(), source_id


def _linkedin(item: dict[str, Any]) -> tuple[str, str, str, str]:
    url = str(item.get("url") or item.get("postUrl") or item.get("link") or "").strip()
    title = str(item.get("title") or item.get("authorName") or item.get("author") or "LinkedIn post")
    body = str(item.get("text") or item.get("commentary") or item.get("content") or item.get("postText") or "")
    source_id = str(item.get("urn") or item.get("id") or url)
    return url, title, f"{title}\n{body}".strip(), source_id


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _public_http(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
