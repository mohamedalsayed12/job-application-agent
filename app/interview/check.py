"""Ground interview packs: every resource URL must appear in collected evidence."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def ground_pack(pack: dict[str, Any], evidence_urls: set[str]) -> list[str]:
    """Return problems; empty means the pack is grounded."""
    allowed = {_norm(url) for url in evidence_urls if url}
    problems: list[str] = []
    for section in ("success_themes", "failure_themes", "differentiators"):
        for theme in pack.get(section) or []:
            if not isinstance(theme, dict):
                problems.append(f"{section}: theme is not an object")
                continue
            for cite in theme.get("cites") or []:
                if not isinstance(cite, str) or _norm(cite) not in allowed:
                    problems.append(f"{section}: cite not in evidence: {cite!r}")
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
        for url in step.get("resource_urls") or []:
            if not isinstance(url, str) or _norm(url) not in allowed:
                problems.append(f"practice_plan: resource url not in evidence: {url!r}")
    summary = pack.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary: missing")
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
