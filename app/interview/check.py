"""Ground interview packs: every claim cites a collected URL, and every quote is in that source."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_QUOTE = re.compile(r'"([^"]{12,})"')


def ground_pack(
    pack: dict[str, Any],
    evidence_urls: set[str],
    evidence_text: dict[str, str] | None = None,
) -> list[str]:
    """Return problems; empty means the pack is grounded."""
    allowed = {_norm(url) for url in evidence_urls if url}
    texts = {_norm(url): text for url, text in (evidence_text or {}).items()}
    problems: list[str] = []
    for section in ("success_themes", "failure_themes", "differentiators"):
        for theme in pack.get(section) or []:
            if not isinstance(theme, dict):
                problems.append(f"{section}: theme is not an object")
                continue
            cites = [cite for cite in theme.get("cites") or [] if isinstance(cite, str) and cite.strip()]
            if not cites:
                problems.append(f"{section}: claim has no evidence url")
            for cite in theme.get("cites") or []:
                if not isinstance(cite, str) or _norm(cite) not in allowed:
                    problems.append(f"{section}: cite not in evidence: {cite!r}")
            if evidence_text is not None:
                cited = "\n".join(texts.get(_norm(cite), "") for cite in cites)
                for quote in _QUOTE.findall(str(theme.get("detail") or "")):
                    if quote not in cited:
                        problems.append(f"{section}: quote is not in the cited source: {quote!r}")
    for material in pack.get("materials") or []:
        if not isinstance(material, dict):
            problems.append("materials: item is not an object")
            continue
        url = material.get("url")
        if not isinstance(url, str) or _norm(url) not in allowed:
            problems.append(f"materials: url not in evidence: {url!r}")
    for step in pack.get("practice_plan") or []:
        if not isinstance(step, dict):
            problems.append("practice_plan: step is not an object")
            continue
        urls = [url for url in step.get("resource_urls") or [] if isinstance(url, str) and url.strip()]
        if not urls:
            problems.append("practice_plan: step has no evidence url")
        for url in step.get("resource_urls") or []:
            if not isinstance(url, str) or _norm(url) not in allowed:
                problems.append(f"practice_plan: resource url not in evidence: {url!r}")
    summary = pack.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary: missing")
    elif evidence_text is not None:
        blob = "\n".join(texts.values())
        for quote in _QUOTE.findall(summary):
            if quote not in blob:
                problems.append(f"summary: quote is not in the sources: {quote!r}")
    return problems


def _norm(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().casefold()
    path = parsed.path.rstrip("/") or "/"
    # YouTube (and similar) put the identity in the query; dropping it would equate every watch URL.
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))
