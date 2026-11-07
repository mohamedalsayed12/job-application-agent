"""Stage 5 step 2: ownership evidence levels (strong / medium / weak)."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

from app.extraction.urls import parse_board_from_url
from app.fetch.gateway import FetchGateway
from app.models import Candidate, EvidenceLevel, VerificationOutcome
from app.queue.names import clean_company_name
from app.verify.html import extract_hrefs
from app.verify.pages import fetch_official_page
from app.verify.types import CheckRecord, OwnershipAssessment

_BOARD_PATH_HINTS = ("/careers", "/jobs")
_MAX_EXTRA_PAGES = 2
_BOARD_URL_RE = re.compile(
    r"https?://(?:boards(?:\.eu)?|job-boards(?:\.eu)?)\.greenhouse\.io/[^\s\"'<>]+"
    r"|https?://jobs(?:\.eu)?\.lever\.co/[^\s\"'<>]+",
    re.IGNORECASE,
)

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
        )
        if strong is not None:
            return OwnershipAssessment(
                level=EvidenceLevel.strong,
                record=CheckRecord(
                    check_type="ownership",
                    outcome=VerificationOutcome.pass_,
                    evidence_url=strong[0],
                    evidence_snippet=strong[1],
                ),
            )

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
    return None
