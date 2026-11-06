"""Write code extractions as ``candidate`` + ``candidate_evidence`` rows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.names import clean_company_name
from app.extraction.types import CodeHit
from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    Candidate,
    CandidateEvidence,
    CandidateState,
    ExtractedBy,
)


def persist_code_hits(session: Session, hits: list[CodeHit]) -> list[Candidate]:
    """Insert or attach evidence for each hit. Name-only dedupe is Step 8."""
    touched: list[Candidate] = []
    for hit in hits:
        candidate = _resolve_candidate(session, hit)
        if candidate not in session:
            session.add(candidate)
        session.flush()
        _add_evidence(session, candidate, hit)
        if candidate not in touched:
            touched.append(candidate)
    return touched


def _resolve_candidate(session: Session, hit: CodeHit) -> Candidate:
    if hit.board is not None:
        existing = session.scalars(
            select(Candidate).where(
                Candidate.board_system == hit.board.system,
                Candidate.board_instance == hit.board.instance,
                Candidate.board_id == hit.board.board_id,
                Candidate.state.notin_(tuple(CANDIDATE_LIVE_UNIQUE_EXCLUDES)),
            )
        ).first()
        if existing is not None:
            if hit.company_name and existing.company_name == existing.board_id:
                existing.company_name = hit.company_name
                existing.cleaned_name = clean_company_name(hit.company_name)
            return existing

    company_name = hit.company_name or (hit.board.board_id if hit.board else "")
    return Candidate(
        company_name=company_name,
        cleaned_name=clean_company_name(company_name),
        board_system=hit.board.system if hit.board else None,
        board_instance=hit.board.instance if hit.board else None,
        board_id=hit.board.board_id if hit.board else None,
        state=CandidateState.new,
        extracted_by=ExtractedBy.code,
    )


def _add_evidence(session: Session, candidate: Candidate, hit: CodeHit) -> None:
    session.add(
        CandidateEvidence(
            candidate_id=candidate.id,
            search_result_id=hit.search_result_id,
            quoted_text=hit.quoted_text[:200],
            source_url=hit.source_url or hit.apply_url or "",
        )
    )
