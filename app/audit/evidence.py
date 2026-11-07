"""Collect evidence URLs for an audit row. Links only — no classification judgment."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidateEvidence, VerificationCheck


def evidence_urls_for(session: Session, candidate_id: UUID) -> tuple[str, ...]:
    """Unique evidence URLs from candidate_evidence and verification_check, stable order."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(url: str | None) -> None:
        if not url:
            return
        cleaned = url.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        ordered.append(cleaned)

    for row in session.scalars(
        select(CandidateEvidence)
        .where(CandidateEvidence.candidate_id == candidate_id)
        .order_by(CandidateEvidence.created_at, CandidateEvidence.id)
    ):
        _add(row.source_url)

    for row in session.scalars(
        select(VerificationCheck)
        .where(VerificationCheck.candidate_id == candidate_id)
        .order_by(VerificationCheck.checked_at, VerificationCheck.id)
    ):
        _add(row.evidence_url)

    return tuple(ordered)
