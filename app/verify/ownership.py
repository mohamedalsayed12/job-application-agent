"""Stage 5 step 2: ownership evidence levels (strong / medium / weak)."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.models import Candidate, EvidenceLevel, VerificationOutcome
from app.queue.names import clean_company_name
from app.verify.html import extract_hrefs
from app.verify.types import CheckRecord, OwnershipAssessment

_RETRYABLE = frozenset({"timeout", "http_error"})
_BOARD_PATH_HINTS = ("/careers", "/careers/", "/jobs", "/jobs/")


async def assess_ownership(
    gateway: FetchGateway,
    candidate: Candidate,
    *,
    domain: str,
    homepage_html: str | None,
    homepage_url: str | None,
    api_company_names: tuple[str, ...] = (),
    api_job_ids: tuple[str, ...] = (),
) -> OwnershipAssessment | str:
    """Return OwnershipAssessment, or 'retry' when a page fetch times out."""
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
) -> list[tuple[str, str]] | str:
    pages: list[tuple[str, str]] = []
    if homepage_html is not None and homepage_url is not None:
        pages.append((homepage_url, homepage_html))
    for path in _BOARD_PATH_HINTS:
        url = f"https://{domain}{path}"
        if any(p[0].rstrip("/") == url.rstrip("/") for p in pages):
            continue
        outcome = await gateway.fetch(
            FetchRequest(url=url, kind="page", official_domain=domain)
        )
        if isinstance(outcome, FetchRefusal):
            if outcome.reason in _RETRYABLE:
                return "retry"
            continue
        assert isinstance(outcome, FetchResponse)
        if outcome.status_code == 200:
            pages.append(
                (outcome.url, outcome.body.decode("utf-8", errors="replace"))
            )
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
        parsed = urlparse(href)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = unquote(parsed.path).lower()
        if system == "greenhouse":
            if "greenhouse.io" in host and f"/{needle}" in f"{path}/":
                return href, f"link_to_board:{needle}"[:200]
            query = parse_qs(parsed.query)
            if "gh_jid" in query and job_ids:
                for value in query["gh_jid"]:
                    if value in job_ids:
                        return href, f"gh_jid:{value}"[:200]
        elif system == "lever":
            if host in {"jobs.lever.co", "jobs.eu.lever.co"} and f"/{needle}" in f"{path}/":
                return href, f"link_to_board:{needle}"[:200]
    # Also scan raw HTML for board URLs (buttons, script embeds).
    lowered = html.lower()
    if system == "greenhouse" and (
        f"boards.greenhouse.io/{needle}" in lowered
        or f"job-boards.greenhouse.io/{needle}" in lowered
    ):
        return page_url, f"embed_or_text_board:{needle}"[:200]
    if system == "lever" and (
        f"jobs.lever.co/{needle}" in lowered or f"jobs.eu.lever.co/{needle}" in lowered
    ):
        return page_url, f"embed_or_text_board:{needle}"[:200]
    return None
