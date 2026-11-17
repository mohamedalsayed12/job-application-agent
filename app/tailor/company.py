"""Fetch and cache employer mission / vision / values from public pages (D55)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.fetch.policy import BLOCKED_SUFFIXES, host_matches_suffix
from app.models import CompanyResearch, CompanyResearchStatus
from app.tailor.refiner import job_keyword_terms

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


NO_SITE = "No employer website was named in the advert or the apply link."
NO_LOAD = "The employer website did not load."
NO_TITLE = "The website title did not name the company, so its wording was not used."
NO_WORDS = "The employer website did not repeat any wording we could use."

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_URL = re.compile(r"https?://[^\s)>\"]+")
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{4,}")
_TERM_STOP = frozenset(
    "about company their with from this that have been were your into also they them just more "
    "than over under after before there these those using through where when what which while "
    "would could should and the for are was not you all can will our team work jobs career "
    "careers people years year".split()
)


@dataclass(frozen=True, slots=True)
class SiteTerms:
    row: CompanyResearch | None
    terms: list[str]
    note: str


def employer_domains(apply_url: str | None, named_url: str | None, posting: str) -> list[str]:
    """Employer hosts in the order to try, never a job board."""
    found: list[str] = []
    for candidate in (apply_url, named_url, *_URL.findall(posting)):
        domain = official_domain_from_apply_url(candidate)
        if domain and domain not in found:
            found.append(domain)
    return found


def employer_domain(apply_url: str | None, named_url: str | None, posting: str) -> str | None:
    """The employer's own host, never a job board."""
    domains = employer_domains(apply_url, named_url, posting)
    return domains[0] if domains else None


def title_names_company(title: str, company_name: str) -> bool:
    tokens = [token.casefold() for token in re.findall(r"[A-Za-z0-9]{4,}", company_name)]
    if not tokens and len(company_name.strip()) >= 3:
        tokens = [company_name.strip().casefold()]
    words = {word.casefold() for word in re.findall(r"[A-Za-z0-9]{3,}", title)}
    return any(token in words for token in tokens)


def terms_from_text(text: str, *, company_name: str, limit: int = 8) -> list[str]:
    """Words the employer's pages repeat, in the casing of their first use."""
    skipped = {token.casefold() for token in re.findall(r"[A-Za-z0-9]{4,}", company_name)}
    counts: dict[str, int] = {}
    casing: dict[str, str] = {}
    for word in _TERM.findall(text):
        key = word.casefold()
        if key in _TERM_STOP or key in skipped:
            continue
        counts[key] = counts.get(key, 0) + 1
        casing.setdefault(key, word)
    ranked = sorted((key for key, count in counts.items() if count >= 2), key=lambda key: (-counts[key], key))
    return [casing[key] for key in ranked[:limit]]


def with_keyword_sources(keywords: dict, company_terms: list[str], note: str) -> dict:
    """Each term keeps where it was read from."""
    advert = job_keyword_terms(keywords)
    company_keys = {term.casefold() for term in company_terms}
    advert_keys = {term.casefold() for term in advert}
    sources = [
        {"term": term, "source": "advert and company" if term.casefold() in company_keys else "advert"}
        for term in advert
    ]
    sources.extend(
        {"term": term, "source": "company"} for term in company_terms if term.casefold() not in advert_keys
    )
    return {**keywords, "company_keywords": list(company_terms), "sources": sources, "company_note": note}


async def load_site_terms(
    session: Session,
    gateway: FetchGateway,
    *,
    company_name: str,
    apply_url: str | None,
    named_url: str | None,
    posting: str,
    now: datetime | None = None,
) -> SiteTerms:
    """Wording from the employer's site, cached per company. A failed read is not cached."""
    now = now or datetime.now(UTC)
    if not company_name.strip():
        return SiteTerms(None, [], NO_SITE)
    domains = employer_domains(apply_url, named_url, posting)
    row = session.scalars(select(CompanyResearch).where(CompanyResearch.company_key == company_key(company_name))).first()
    if not domains:
        return SiteTerms(row, [], NO_SITE)
    for domain in domains:
        cached = _cached_terms(row, domain, now)
        if cached is not None:
            return SiteTerms(row, cached[0], cached[1])
    note = NO_TITLE
    for domain in domains:
        home = await _page(gateway, f"https://{domain}/", domain)
        if home is None:
            note = NO_LOAD
            continue
        raw, text = home
        if not title_names_company(_title_text(raw), company_name):
            note = NO_TITLE
            continue
        pages = await _fetch_about_pages(gateway, domain)
        blob = "\n".join([text, *(page for _, page in pages)])
        terms = terms_from_text(blob, company_name=company_name)
        saved_note = "" if terms else NO_WORDS
        saved = _save_terms(
            session, row, company_name=company_name, domain=domain, terms=terms, note=saved_note, now=now
        )
        return SiteTerms(saved, terms, saved_note)
    return SiteTerms(row, [], note)


def _title_text(raw: str) -> str:
    match = _TITLE.search(raw)
    if match is None:
        return ""
    return _SPACE.sub(" ", _TAG.sub(" ", match.group(1))).strip()


def _cached_terms(row: CompanyResearch | None, domain: str, now: datetime) -> tuple[list[str], str] | None:
    if row is None or not isinstance(row.site_terms, dict) or row.site_terms.get("domain") != domain:
        return None
    raw = row.site_terms.get("fetched_at")
    if not isinstance(raw, str):
        return None
    try:
        fetched = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    if now - fetched > TTL:
        return None
    terms = row.site_terms.get("terms")
    if not isinstance(terms, list):
        return None
    note = row.site_terms.get("note")
    return [term for term in terms if isinstance(term, str)], note if isinstance(note, str) else ""


def _save_terms(
    session: Session,
    row: CompanyResearch | None,
    *,
    company_name: str,
    domain: str,
    terms: list[str],
    note: str,
    now: datetime,
) -> CompanyResearch:
    payload = {"terms": terms, "domain": domain, "fetched_at": now.isoformat(), "note": note}
    if row is None:
        # fetched_at stays stale so interview research still loads the About pages itself.
        row = CompanyResearch(
            company_key=company_key(company_name),
            company_name=company_name.strip() or company_name,
            status=CompanyResearchStatus.unavailable.value,
            research=_empty_research(),
            official_domain=None,
            fetched_at=now - TTL - timedelta(days=1),
            site_terms=payload,
        )
        session.add(row)
    else:
        row.site_terms = payload
        row.updated_at = now
    session.flush()
    return row


async def _page(gateway: FetchGateway, url: str, domain: str) -> tuple[str, str] | None:
    outcome = await gateway.fetch(FetchRequest(url=url, kind="page", official_domain=domain, timeout_s=15.0))
    if isinstance(outcome, FetchRefusal) or not isinstance(outcome, FetchResponse):
        return None
    if outcome.status_code != 200 or not outcome.body:
        return None
    raw = outcome.body.decode("utf-8", errors="replace")
    return raw, html_to_text(outcome.body, max_chars=8000)


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
