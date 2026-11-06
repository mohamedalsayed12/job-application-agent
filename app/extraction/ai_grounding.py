"""Code grounding checks for AI leftover extraction (MVP §10.4–5 / T11)."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from app.extraction.ai_types import (
    AiEvidenceItem,
    AiSuggestion,
    GroundedHit,
    LeftoverResult,
    UngroundedItem,
)
from app.queue.domains import normalize_domain

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b",
    re.IGNORECASE,
)


def ground_suggestions(
    suggestions: Sequence[AiSuggestion],
    results: Sequence[LeftoverResult],
) -> tuple[list[GroundedHit], list[UngroundedItem]]:
    by_id: dict[uuid.UUID, LeftoverResult] = {row.id: row for row in results}
    grounded: list[GroundedHit] = []
    ungrounded: list[UngroundedItem] = []

    for suggestion in suggestions:
        hit, rejects = _ground_one(suggestion, by_id)
        for item in rejects:
            ungrounded.append(item)
            logger.info(
                "ungrounded ai extraction: reason=%s company=%s detail=%s",
                item.reason,
                item.company_name,
                item.detail,
            )
        if hit is not None:
            grounded.append(hit)
    return grounded, ungrounded


def _ground_one(
    suggestion: AiSuggestion,
    by_id: Mapping[uuid.UUID, LeftoverResult],
) -> tuple[GroundedHit | None, list[UngroundedItem]]:
    rejects: list[UngroundedItem] = []
    company = suggestion.company_name.strip()
    if not company:
        rejects.append(
            UngroundedItem(reason="empty_company_name", company_name=None)
        )
        return None, rejects
    if not suggestion.evidence:
        rejects.append(
            UngroundedItem(reason="missing_evidence", company_name=company)
        )
        return None, rejects

    accepted_evidence: list[AiEvidenceItem] = []
    for item in suggestion.evidence:
        ok, reason = _evidence_ok(item, by_id)
        if not ok:
            rejects.append(
                UngroundedItem(
                    reason=reason or "bad_evidence",
                    company_name=company,
                    detail=str(item.result_id),
                )
            )
            continue
        accepted_evidence.append(
            AiEvidenceItem(result_id=item.result_id, quote=item.quote[:200])
        )

    if not accepted_evidence:
        return None, rejects

    if not any(company.lower() in ev.quote.lower() for ev in accepted_evidence):
        rejects.append(
            UngroundedItem(
                reason="company_not_in_quotes",
                company_name=company,
            )
        )
        return None, rejects

    corpus = _corpus_text(by_id, accepted_evidence)
    corpus_domains = _domains_in_text(corpus)
    accepted_domains: list[str] = []
    for raw in suggestion.domain_candidates:
        domain = normalize_domain(raw)
        if domain is None:
            rejects.append(
                UngroundedItem(
                    reason="invalid_domain",
                    company_name=company,
                    detail=raw,
                )
            )
            continue
        if domain not in corpus_domains and not _domain_in_urls(
            domain, by_id, accepted_evidence
        ):
            rejects.append(
                UngroundedItem(
                    reason="domain_not_in_results",
                    company_name=company,
                    detail=domain,
                )
            )
            continue
        accepted_domains.append(domain)

    # Domains are optional: a name+quote hit can stand alone. Rejected domains
    # stay in ungrounded; the employer may still ground without them.
    return (
        GroundedHit(
            company_name=company,
            domain_candidates=tuple(accepted_domains),
            evidence=tuple(accepted_evidence),
        ),
        rejects,
    )


def _evidence_ok(
    item: AiEvidenceItem,
    by_id: Mapping[uuid.UUID, LeftoverResult],
) -> tuple[bool, str | None]:
    row = by_id.get(item.result_id)
    if row is None:
        return False, "unknown_result_id"
    quote = item.quote.strip()
    if not quote or len(quote) > 200:
        return False, "bad_quote"
    haystack = f"{row.url}\n{row.title}\n{row.snippet}"
    if quote not in haystack:
        return False, "quote_not_in_result"
    return True, None


def _corpus_text(
    by_id: Mapping[uuid.UUID, LeftoverResult],
    evidence: Sequence[AiEvidenceItem],
) -> str:
    parts: list[str] = []
    for item in evidence:
        row = by_id[item.result_id]
        parts.append(f"{row.url}\n{row.title}\n{row.snippet}")
    return "\n".join(parts).lower()


def _domains_in_text(text: str) -> frozenset[str]:
    """Token-level domains only — never accept substring hosts like ample.com⊏example.com."""
    found: set[str] = set()
    for match in _DOMAIN_RE.finditer(text):
        host = match.group(0).lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        found.add(host)
    return frozenset(found)


def _domain_in_urls(
    domain: str,
    by_id: Mapping[uuid.UUID, LeftoverResult],
    evidence: Sequence[AiEvidenceItem],
) -> bool:
    domain = domain.lower()
    for item in evidence:
        host = urlparse(by_id[item.result_id].url).hostname
        if host is None:
            continue
        host = host.lower().rstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def parse_suggestions(payload: object) -> tuple[list[AiSuggestion], list[UngroundedItem]]:
    """Parse model JSON at the boundary into domain suggestions."""
    rejects: list[UngroundedItem] = []
    if not isinstance(payload, dict):
        rejects.append(UngroundedItem(reason="invalid_payload_type"))
        return [], rejects
    raw_employers = payload.get("employers")
    if not isinstance(raw_employers, list):
        rejects.append(UngroundedItem(reason="missing_employers"))
        return [], rejects

    suggestions: list[AiSuggestion] = []
    for entry in raw_employers:
        parsed = _parse_one_employer(entry)
        if isinstance(parsed, UngroundedItem):
            rejects.append(parsed)
            continue
        suggestions.append(parsed)
    return suggestions, rejects


def _parse_one_employer(entry: object) -> AiSuggestion | UngroundedItem:
    if not isinstance(entry, dict):
        return UngroundedItem(reason="employer_not_object")
    name = entry.get("company_name")
    if not isinstance(name, str):
        return UngroundedItem(reason="bad_company_name")
    domains_raw = entry.get("domain_candidates", [])
    if not isinstance(domains_raw, list):
        return UngroundedItem(reason="bad_domain_candidates", company_name=name)
    domains: list[str] = []
    for item in domains_raw:
        if isinstance(item, str):
            domains.append(item)
        else:
            return UngroundedItem(
                reason="bad_domain_candidate",
                company_name=name,
            )
    evidence_raw = entry.get("evidence", [])
    if not isinstance(evidence_raw, list):
        return UngroundedItem(reason="bad_evidence_list", company_name=name)
    evidence: list[AiEvidenceItem] = []
    for item in evidence_raw:
        if not isinstance(item, dict):
            return UngroundedItem(reason="bad_evidence_item", company_name=name)
        result_id_raw = item.get("result_id")
        quote = item.get("quote")
        if not isinstance(result_id_raw, str) or not isinstance(quote, str):
            return UngroundedItem(reason="bad_evidence_fields", company_name=name)
        try:
            result_id = uuid.UUID(result_id_raw)
        except ValueError:
            return UngroundedItem(
                reason="malformed_result_id",
                company_name=name,
                detail=result_id_raw,
            )
        evidence.append(AiEvidenceItem(result_id=result_id, quote=quote[:200]))
    return AiSuggestion(
        company_name=name,
        domain_candidates=tuple(domains),
        evidence=tuple(evidence),
    )
