"""Stage 5 step 1: confirm an official domain from stored evidence only."""

from __future__ import annotations

import re

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.models import Candidate, VerificationOutcome
from app.queue.domains import normalize_domain
from app.queue.names import clean_company_name
from app.verify.blocklist import is_aggregator_domain
from app.verify.html import page_title
from app.verify.types import CheckRecord, OfficialDomainCheck

_RETRYABLE_REFUSALS = frozenset({"timeout", "http_error"})
_PHRASE = re.compile(r"\s+")


def candidate_domains(candidate: Candidate) -> list[str]:
    """Domains already on the candidate row — never invent new ones."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in (candidate.primary_domain, *(candidate.domain_candidates or ())):
        domain = normalize_domain(raw)
        if domain is None or domain in seen:
            continue
        seen.add(domain)
        ordered.append(domain)
    return ordered


def names_match(company_name: str, page_title_text: str) -> bool:
    """Whole cleaned phrase match (word boundaries), not bare substring."""
    cleaned_company = clean_company_name(company_name)
    cleaned_title = clean_company_name(page_title_text)
    if not cleaned_company or not cleaned_title:
        return False
    if cleaned_company == cleaned_title:
        return True
    pattern = re.compile(
        rf"(?:^|\s){re.escape(cleaned_company)}(?:\s|$)",
        re.IGNORECASE,
    )
    return pattern.search(_PHRASE.sub(" ", cleaned_title)) is not None


async def confirm_official_domain(
    gateway: FetchGateway,
    candidate: Candidate,
) -> OfficialDomainCheck:
    domains = candidate_domains(candidate)
    if not domains:
        return OfficialDomainCheck(
            confirmed=False,
            domain=None,
            record=CheckRecord(
                check_type="official_domain",
                outcome=VerificationOutcome.fail,
                evidence_snippet="no_domain_in_evidence",
            ),
        )

    last_fail: OfficialDomainCheck | None = None
    for domain in domains:
        result = await _check_one_domain(gateway, candidate, domain)
        if result.confirmed:
            return result
        if result.record.outcome == VerificationOutcome.error:
            return result
        last_fail = result
    assert last_fail is not None
    return last_fail


async def _check_one_domain(
    gateway: FetchGateway,
    candidate: Candidate,
    domain: str,
) -> OfficialDomainCheck:
    if is_aggregator_domain(domain):
        return OfficialDomainCheck(
            confirmed=False,
            domain=domain,
            record=CheckRecord(
                check_type="official_domain",
                outcome=VerificationOutcome.fail,
                evidence_url=f"https://{domain}/",
                evidence_snippet="aggregator_blocklist",
            ),
        )

    url = f"https://{domain}/"
    outcome = await gateway.fetch(
        FetchRequest(url=url, kind="page", official_domain=domain)
    )
    if isinstance(outcome, FetchRefusal):
        if outcome.reason in _RETRYABLE_REFUSALS:
            return OfficialDomainCheck(
                confirmed=False,
                domain=domain,
                record=CheckRecord(
                    check_type="official_domain",
                    outcome=VerificationOutcome.error,
                    evidence_url=url,
                    evidence_snippet=outcome.reason,
                ),
            )
        return OfficialDomainCheck(
            confirmed=False,
            domain=domain,
            record=CheckRecord(
                check_type="official_domain",
                outcome=VerificationOutcome.fail,
                evidence_url=url,
                evidence_snippet=outcome.reason,
            ),
        )

    assert isinstance(outcome, FetchResponse)
    if outcome.status_code != 200:
        return OfficialDomainCheck(
            confirmed=False,
            domain=domain,
            record=CheckRecord(
                check_type="official_domain",
                outcome=VerificationOutcome.fail,
                evidence_url=outcome.url,
                evidence_snippet=f"http_{outcome.status_code}",
                http_status=outcome.status_code,
            ),
        )

    html = outcome.body.decode("utf-8", errors="replace")
    title = page_title(html) or ""
    if not names_match(candidate.company_name, title):
        return OfficialDomainCheck(
            confirmed=False,
            domain=domain,
            record=CheckRecord(
                check_type="official_domain",
                outcome=VerificationOutcome.fail,
                evidence_url=outcome.url,
                evidence_snippet=(title[:200] or "title_mismatch"),
                http_status=outcome.status_code,
            ),
        )

    return OfficialDomainCheck(
        confirmed=True,
        domain=domain,
        record=CheckRecord(
            check_type="official_domain",
            outcome=VerificationOutcome.pass_,
            evidence_url=outcome.url,
            evidence_snippet=title[:200],
            http_status=outcome.status_code,
        ),
        homepage_html=html,
        homepage_url=outcome.url,
    )
