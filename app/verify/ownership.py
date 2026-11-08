"""Stage 5 step 2: ownership evidence levels (strong / medium / weak)."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

from app.extraction.urls import parse_board_from_url
from app.fetch.gateway import FetchGateway
from app.models import Candidate, EvidenceLevel, VerificationOutcome
from app.queue.names import clean_company_name
from app.verify.html import extract_hrefs, extract_script_srcs, host_of
from app.verify.pages import fetch_official_page
from app.verify.types import CheckRecord, OwnershipAssessment

_BOARD_PATH_HINTS = ("/careers", "/jobs")
_MAX_EXTRA_PAGES = 2
_BOARD_URL_RE = re.compile(
    r"https?://(?:boards(?:\.eu)?|job-boards(?:\.eu)?)\.greenhouse\.io/[^\s\"'<>]+"
    r"|https?://jobs(?:\.eu)?\.lever\.co/[^\s\"'<>]+",
    re.IGNORECASE,
)

_BOARD_API_RE = {
    "greenhouse": re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_.-]+)", re.IGNORECASE),
    "lever": re.compile(r"api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", re.IGNORECASE),
}
_JOB_ID_RE = re.compile(
    r"(?<!\d)\d{6,}(?!\d)|(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
_MIN_OWN_JOB_LINKS = 3
_MAX_SCRIPTS = 8
_JOB_SCRIPT_HINT = re.compile(r"greenhouse|lever|job|career|recruit|vacanc", re.IGNORECASE)

OwnershipResult = OwnershipAssessment | Literal["retry"]


async def assess_ownership(
    gateway: FetchGateway,
    candidate: Candidate,
    *,
    domain: str,
    homepage_html: str | None,
    homepage_url: str | None,
    api_company_names: tuple[str, ...] = (),
    api_job_ids: tuple[str, ...] = (),
) -> OwnershipResult:
    """Return ownership level, or ``\"retry\"`` when a page fetch times out."""
    board_system = candidate.board_system
    board_id = candidate.board_id
    if not board_system or not board_id:
        return OwnershipAssessment(
            level=EvidenceLevel.weak,
            record=CheckRecord(
                check_type="ownership",
                outcome=VerificationOutcome.inconclusive,
                evidence_snippet="no_board_on_candidate",
            ),
        )

    pages = await _collect_pages(
        gateway,
        domain=domain,
        homepage_html=homepage_html,
        homepage_url=homepage_url,
    )
    if pages == "retry":
        return "retry"

    for url, html in pages:
        strong = _strong_from_page(
            url,
            html,
            system=board_system,
            board_id=board_id,
            job_ids=api_job_ids,
        ) or _strong_from_own_job_links(url, html, domain=domain, job_ids=api_job_ids)
        if strong is not None:
            return _strong_assessment(strong)

    strong = await _strong_from_scripts(gateway, pages, domain=domain, system=board_system, board_id=board_id)
    if strong is not None:
        return _strong_assessment(strong)

    # Greenhouse medium: board's own company_name matches; Lever has no name field.
    if board_system == "greenhouse" and api_company_names:
        cleaned = clean_company_name(candidate.company_name)
        for raw in api_company_names:
            if cleaned and cleaned == clean_company_name(raw):
                return OwnershipAssessment(
                    level=EvidenceLevel.medium,
                    record=CheckRecord(
                        check_type="ownership",
                        outcome=VerificationOutcome.inconclusive,
                        evidence_snippet=f"greenhouse_name_match:{raw}"[:200],
                    ),
                )

    return OwnershipAssessment(
        level=EvidenceLevel.weak,
        record=CheckRecord(
            check_type="ownership",
            outcome=VerificationOutcome.inconclusive,
            evidence_snippet="weak_or_no_link",
        ),
    )


def _strong_assessment(strong: tuple[str, str]) -> OwnershipAssessment:
    return OwnershipAssessment(
        level=EvidenceLevel.strong,
        record=CheckRecord(
            check_type="ownership",
            outcome=VerificationOutcome.pass_,
            evidence_url=strong[0],
            evidence_snippet=strong[1],
        ),
    )


async def _collect_pages(
    gateway: FetchGateway,
    *,
    domain: str,
    homepage_html: str | None,
    homepage_url: str | None,
) -> list[tuple[str, str]] | Literal["retry"]:
    pages: list[tuple[str, str]] = []
    if homepage_html is not None and homepage_url is not None:
        pages.append((homepage_url, homepage_html))
    for path in _BOARD_PATH_HINTS:
        if len(pages) > _MAX_EXTRA_PAGES:
            break
        url = f"https://{domain}{path}"
        if any(p[0].rstrip("/") == url.rstrip("/") for p in pages):
            continue
        fetched = await fetch_official_page(gateway, url, domain=domain)
        if fetched.kind == "retry":
            return "retry"
        if fetched.kind == "ok" and fetched.url and fetched.html is not None:
            pages.append((fetched.url, fetched.html))
    return pages


def _strong_from_page(
    page_url: str,
    html: str,
    *,
    system: str,
    board_id: str,
    job_ids: tuple[str, ...],
) -> tuple[str, str] | None:
    needle = board_id.lower()
    for href in extract_hrefs(html, base_url=page_url):
        ref = parse_board_from_url(href)
        if ref is not None and ref.system == system and ref.board_id == needle:
            return href, f"link_to_board:{needle}"[:200]
        if system == "greenhouse" and job_ids:
            query = parse_qs(urlparse(href).query)
            for value in query.get("gh_jid", []):
                if value in job_ids:
                    return href, f"gh_jid:{value}"[:200]
    for match in _BOARD_URL_RE.findall(html):
        ref = parse_board_from_url(match)
        if ref is not None and ref.system == system and ref.board_id == needle:
            return page_url, f"embed_or_text_board:{needle}"[:200]
    if _calls_board_api(html, system=system, board_id=needle):
        return page_url, f"inline_script_board:{needle}"[:200]
    return None


def _calls_board_api(text: str, *, system: str, board_id: str) -> bool:
    pattern = _BOARD_API_RE.get(system)
    if pattern is None:
        return False
    return any(found.lower() == board_id for found in pattern.findall(text))


def _on_domain(url: str, domain: str) -> bool:
    host = host_of(url)
    return host is not None and (host == domain or host.endswith(f".{domain}"))


def _strong_from_own_job_links(
    page_url: str,
    html: str,
    *,
    domain: str,
    job_ids: tuple[str, ...],
) -> tuple[str, str] | None:
    """The company's own links carry the board's job ids, as Deliveroo's role pages do."""
    if not job_ids:
        return None
    known = set(job_ids)
    found: set[str] = set()
    for href in extract_hrefs(html, base_url=page_url):
        if _on_domain(href, domain):
            parsed = urlparse(href.lower())
            found.update(known.intersection(_JOB_ID_RE.findall(f"{parsed.path}?{parsed.query}")))
    if len(found) < min(_MIN_OWN_JOB_LINKS, len(known)):
        return None
    return page_url, f"own_job_links:{len(found)}"[:200]


async def _strong_from_scripts(
    gateway: FetchGateway,
    pages: list[tuple[str, str]],
    *,
    domain: str,
    system: str,
    board_id: str,
) -> tuple[str, str] | None:
    """Read scripts on the company's own domain as text; they are never run."""
    needle = board_id.lower()
    sources = {
        src: None
        for url, html in pages
        for src in extract_script_srcs(html, base_url=url)
        if _on_domain(src, domain)
    }
    named_first = sorted(sources, key=lambda src: not _JOB_SCRIPT_HINT.search(src))
    for src in named_first[:_MAX_SCRIPTS]:
        fetched = await fetch_official_page(gateway, src, domain=domain)
        if fetched.kind == "ok" and fetched.html and _calls_board_api(fetched.html, system=system, board_id=needle):
            return src, f"script_calls_board:{needle}"[:200]
    return None
