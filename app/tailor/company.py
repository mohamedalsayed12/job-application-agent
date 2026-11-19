"""Fetch and cache employer mission / vision / values from public pages (D60)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.fetch.policy import BLOCKED_SUFFIXES, host_matches_suffix
from app.models import CompanyResearch, CompanyResearchStatus

PROMPT_VERSION = "company-research-v1"
TTL = timedelta(days=30)
MAX_PAGE_CHARS = 4000
MAX_TOTAL_CHARS = 12000

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

# Aggregator / ATS hosts are not the employer's about-us site.
_SKIP_HOST_SUFFIXES = BLOCKED_SUFFIXES + (
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "workday.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
)

_ABOUT_PATHS = (
    "/about",
    "/about-us",
    "/company",
    "/mission",
    "/values",
)


def company_key(name: str) -> str:
    return _SPACE.sub(" ", name.strip().lower())


def official_domain_from_apply_url(apply_url: str | None) -> str | None:
    """Apex host from the apply link when it looks like the employer's own site."""
    if not apply_url or not apply_url.lower().startswith(("https://", "http://")):
        return None
    host = (urlparse(apply_url).hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return None
    if any(host_matches_suffix(host, suffix) for suffix in _SKIP_HOST_SUFFIXES):
        return None
    parts = host.split(".")
    # careers.acme.co.uk -> acme.co.uk; www.acme.com -> acme.com
    if host.startswith("www."):
        host = host[4:]
        parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "org", "ac", "gov"} and parts[-1] == "uk":
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return None


def html_to_text(body: bytes, *, max_chars: int = MAX_PAGE_CHARS) -> str:
    try:
        raw = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    text = _TAG.sub(" ", raw)
    text = _SPACE.sub(" ", text).strip()
    return text[:max_chars]


def _empty_research() -> dict:
    return {
        "mission": "",
        "vision": "",
        "goals": "",
        "values": [],
        "evidence": [],
        "source_urls": [],
        "excerpt": "",
    }


async def research_company(
    session: Session,
    gateway: FetchGateway,
    *,
    company_name: str,
    apply_url: str | None,
    now: datetime | None = None,
) -> CompanyResearch:
    """Return cached research, or fetch About pages when an official domain is known."""
    now = now or datetime.now(UTC)
    key = company_key(company_name)
    row = session.scalars(select(CompanyResearch).where(CompanyResearch.company_key == key)).first()
    if row is not None and now - row.fetched_at <= TTL:
        return row

    domain = official_domain_from_apply_url(apply_url)
    research = _empty_research()
    status = CompanyResearchStatus.unavailable
    if domain:
        try:
            pages = await _fetch_about_pages(gateway, domain)
        except Exception:
            pages = []
        if pages:
            status = CompanyResearchStatus.ready
            research = _from_pages(pages)

    if row is None:
        row = CompanyResearch(
            company_key=key,
            company_name=company_name.strip(),
            status=status.value,
            research=research,
            official_domain=domain,
            fetched_at=now,
        )
        session.add(row)
    else:
        row.company_name = company_name.strip()
        row.status = status.value
        row.research = research
        row.official_domain = domain
        row.fetched_at = now
        row.updated_at = now
    session.flush()
    return row


async def _fetch_about_pages(gateway: FetchGateway, domain: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    total = 0
    misses = 0
    for path in _ABOUT_PATHS:
        if total >= MAX_TOTAL_CHARS:
            break
        # Two empty replies in a row usually means the host has no About pages we can read.
        if misses >= 2 and not found:
            break
        url = urljoin(f"https://{domain}/", path.lstrip("/"))
        outcome = await gateway.fetch(
            FetchRequest(url=url, kind="page", official_domain=domain, timeout_s=15.0)
        )
        if isinstance(outcome, FetchRefusal) or not isinstance(outcome, FetchResponse):
            misses += 1
            continue
        if outcome.status_code != 200:
            misses += 1
            continue
        text = html_to_text(outcome.body)
        if len(text) < 80:
            misses += 1
            continue
        misses = 0
        found.append((str(outcome.url), text))
        total += len(text)
        # One solid About page is enough for mission/vision excerpts.
        if len(found) >= 2:
            break
    return found


def _from_pages(pages: list[tuple[str, str]]) -> dict:
    joined = "\n\n".join(text for _, text in pages)[:MAX_TOTAL_CHARS]
    lower = joined.lower()
    research = _empty_research()
    research["source_urls"] = [url for url, _ in pages]
    research["excerpt"] = joined[:MAX_PAGE_CHARS]
    research["mission"] = _snippet(joined, lower, ("mission", "our mission"))
    research["vision"] = _snippet(joined, lower, ("vision", "our vision"))
    research["goals"] = _snippet(joined, lower, ("goal", "our goals", "ambition"))
    values = []
    for label in ("values", "our values", "we believe"):
        hit = _snippet(joined, lower, (label,))
        if hit:
            values.append(hit)
    research["values"] = values[:5]
    research["evidence"] = [
        {"url": url, "quote": text[:240]} for url, text in pages[:3]
    ]
    return research


def _snippet(text: str, lower: str, needles: tuple[str, ...]) -> str:
    for needle in needles:
        idx = lower.find(needle)
        if idx < 0:
            continue
        start = max(0, idx - 40)
        end = min(len(text), idx + 280)
        return text[start:end].strip()
    return ""


def research_for_prompt(row: CompanyResearch) -> str:
    if row.status != CompanyResearchStatus.ready:
        return "Company research unavailable. Do not invent mission, vision or values."
    data = row.research or {}
    parts = [
        f"Company: {row.company_name}",
        f"Domain: {row.official_domain or ''}",
        f"Mission: {data.get('mission') or '(not found)'}",
        f"Vision: {data.get('vision') or '(not found)'}",
        f"Goals: {data.get('goals') or '(not found)'}",
        "Values: " + "; ".join(data.get("values") or []) or "(not found)",
        "Excerpt:\n" + (data.get("excerpt") or "")[:3000],
    ]
    return "\n".join(parts)
