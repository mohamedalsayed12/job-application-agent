"""Four-step candidate dedupe before any expensive verification (MVP §9)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.extraction.names import clean_company_name
from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    Candidate,
    CandidateEvidence,
    CandidateState,
    Employer,
    EmployerBoard,
)
from app.queue.domains import normalize_domain
from app.queue.types import BoardKey, DedupeReason, DedupeResult

_LIVE = tuple(CANDIDATE_LIVE_UNIQUE_EXCLUDES)
_CACHE_STATES = (CandidateState.unsupported, CandidateState.rejected)


def ingest_candidate(
    session: Session,
    *,
    company_name: str,
    extracted_by: str,
    cleaned_name: str | None = None,
    domain_candidates: list[str] | None = None,
    primary_domain: str | None = None,
    board_system: str | None = None,
    board_instance: str | None = None,
    board_id: str | None = None,
    ai_extraction_id: uuid.UUID | None = None,
    evidence: list[tuple[uuid.UUID | None, str, str]] | None = None,
    now: datetime | None = None,
) -> DedupeResult:
    """Insert or merge a candidate through the four-step dedupe; leave queued/duplicate.

    ``evidence`` items are ``(search_result_id, quoted_text, source_url)``.
    """
    stamp = now or datetime.now(UTC)
    domains = [d for d in (normalize_domain(x) for x in (domain_candidates or [])) if d]
    primary = normalize_domain(primary_domain)
    if primary is None and len(domains) == 1:
        primary = domains[0]
    cleaned = cleaned_name or clean_company_name(company_name)
    board = None
    if board_system and board_instance and board_id:
        board = BoardKey(board_system, board_instance, board_id)

    hit = _resolve(session, cleaned=cleaned, primary=primary, board=board, now=stamp)
    if hit.kind == "none":
        survivor = Candidate(
            company_name=company_name,
            cleaned_name=cleaned,
            domain_candidates=domains,
            primary_domain=primary,
            board_system=board.system if board else None,
            board_instance=board.instance if board else None,
            board_id=board.board_id if board else None,
            state=CandidateState.queued,
            extracted_by=extracted_by,
            ai_extraction_id=ai_extraction_id,
            next_attempt_at=stamp,
        )
        session.add(survivor)
        session.flush()
        result = DedupeResult(survivor=survivor, created=True, reason="fresh")
    elif hit.kind == "directory_only":
        survivor = Candidate(
            company_name=company_name,
            cleaned_name=cleaned,
            domain_candidates=domains,
            primary_domain=primary,
            board_system=board.system if board else None,
            board_instance=board.instance if board else None,
            board_id=board.board_id if board else None,
            state=CandidateState.duplicate,
            extracted_by=extracted_by,
            ai_extraction_id=ai_extraction_id,
            employer_id=hit.employer_id,
        )
        session.add(survivor)
        session.flush()
        result = DedupeResult(survivor=survivor, created=True, reason=hit.reason)
    else:
        survivor = hit.candidate
        assert survivor is not None
        _enrich(survivor, company_name=company_name, domains=domains, primary=primary, board=board)
        if ai_extraction_id is not None and survivor.ai_extraction_id is None:
            survivor.ai_extraction_id = ai_extraction_id
        if survivor.state == CandidateState.new:
            survivor.state = CandidateState.queued
            if survivor.next_attempt_at is None:
                survivor.next_attempt_at = stamp
        session.flush()
        result = DedupeResult(survivor=survivor, created=False, reason=hit.reason)

    if evidence:
        for search_result_id, quoted_text, source_url in evidence:
            _add_evidence(
                session,
                result.survivor,
                search_result_id=search_result_id,
                quoted_text=quoted_text,
                source_url=source_url,
            )
    return result


def finalize_new_candidate(
    session: Session,
    candidate: Candidate,
    *,
    now: datetime | None = None,
) -> DedupeResult:
    """Run four-step dedupe on an existing ``new`` row (or re-check a draft)."""
    if candidate.state not in (CandidateState.new, CandidateState.queued):
        return DedupeResult(survivor=candidate, created=False, reason="fresh")

    stamp = now or datetime.now(UTC)
    board = None
    if candidate.board_system and candidate.board_instance and candidate.board_id:
        board = BoardKey(
            candidate.board_system,
            candidate.board_instance,
            candidate.board_id,
        )
    primary = normalize_domain(candidate.primary_domain)
    if primary is None and candidate.domain_candidates:
        normalized = [d for d in (normalize_domain(x) for x in candidate.domain_candidates) if d]
        if len(normalized) == 1:
            primary = normalized[0]
            candidate.primary_domain = primary

    hit = _resolve(
        session,
        cleaned=candidate.cleaned_name,
        primary=primary,
        board=board,
        now=stamp,
        exclude_id=candidate.id,
    )
    if hit.kind == "none":
        candidate.state = CandidateState.queued
        candidate.next_attempt_at = candidate.next_attempt_at or stamp
        if primary is not None:
            candidate.primary_domain = primary
        session.flush()
        return DedupeResult(survivor=candidate, created=False, reason="fresh")
    if hit.kind == "directory_only":
        candidate.state = CandidateState.duplicate
        candidate.employer_id = hit.employer_id
        candidate.claimed_at = None
        candidate.next_attempt_at = None
        session.flush()
        return DedupeResult(survivor=candidate, created=False, reason=hit.reason)

    assert hit.candidate is not None
    _merge_into(session, donor=candidate, survivor=hit.candidate)
    return DedupeResult(survivor=hit.candidate, created=False, reason=hit.reason)


@dataclass(frozen=True, slots=True)
class _ResolveHit:
    kind: Literal["none", "existing", "directory_only"]
    reason: DedupeReason
    candidate: Candidate | None = None
    employer_id: uuid.UUID | None = None


def _resolve(
    session: Session,
    *,
    cleaned: str,
    primary: str | None,
    board: BoardKey | None,
    now: datetime,
    exclude_id: uuid.UUID | None = None,
) -> _ResolveHit:
    # 1. Board key — directory first, then live queue.
    if board is not None:
        cached = _discovery_cache_board(session, board, now=now, exclude_id=exclude_id)
        if cached is not None:
            return _ResolveHit("existing", "discovery_cache", candidate=cached)
        directory = _board_in_directory(session, board)
        if directory is not None:
            linked = _candidate_for_employer_board(session, directory, exclude_id=exclude_id)
            if linked is not None:
                return _ResolveHit("existing", "board_directory", candidate=linked)
            return _ResolveHit(
                "directory_only",
                "board_directory",
                employer_id=directory.employer_id,
            )
        live = _live_by_board(session, board, exclude_id=exclude_id)
        if live is not None:
            return _ResolveHit("existing", "board_live", candidate=live)

    # 2. Official / primary domain — directory then live.
    if primary is not None:
        cached = _discovery_cache_domain(session, primary, now=now, exclude_id=exclude_id)
        if cached is not None:
            return _ResolveHit("existing", "discovery_cache", candidate=cached)
        employer = session.scalars(
            select(Employer).where(Employer.official_domain == primary)
        ).first()
        if employer is not None:
            linked = _candidate_for_employer(session, employer, exclude_id=exclude_id)
            if linked is not None:
                return _ResolveHit("existing", "domain_directory", candidate=linked)
            return _ResolveHit(
                "directory_only",
                "domain_directory",
                employer_id=employer.id,
            )
        live = _live_by_domain(session, primary, exclude_id=exclude_id)
        if live is not None:
            return _ResolveHit("existing", "domain_live", candidate=live)

    # 3. Cleaned name — group only when domains do not conflict.
    #    A matching name alone never marks duplicate across two domains.
    if primary is None and board is None:
        grouped = _live_by_name_no_domain(session, cleaned, exclude_id=exclude_id)
        if grouped is not None:
            return _ResolveHit("existing", "name_group", candidate=grouped)

    return _ResolveHit("none", "fresh")


def _board_in_directory(session: Session, board: BoardKey) -> EmployerBoard | None:
    return session.scalars(
        select(EmployerBoard).where(
            EmployerBoard.system == board.system,
            EmployerBoard.instance == board.instance,
            EmployerBoard.board_id == board.board_id,
        )
    ).first()


def _live_by_board(
    session: Session,
    board: BoardKey,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    stmt = select(Candidate).where(
        Candidate.board_system == board.system,
        Candidate.board_instance == board.instance,
        Candidate.board_id == board.board_id,
        Candidate.state.notin_(_LIVE),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _live_by_domain(
    session: Session,
    domain: str,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    stmt = select(Candidate).where(
        Candidate.primary_domain == domain,
        Candidate.state.notin_(_LIVE),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _live_by_name_no_domain(
    session: Session,
    cleaned: str,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    """Same cleaned name with no board/domain yet — one queue row (T13 grouping)."""
    stmt = select(Candidate).where(
        Candidate.cleaned_name == cleaned,
        Candidate.primary_domain.is_(None),
        Candidate.board_system.is_(None),
        Candidate.state.notin_(_LIVE),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _discovery_cache_board(
    session: Session,
    board: BoardKey,
    *,
    now: datetime,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    stmt = select(Candidate).where(
        Candidate.board_system == board.system,
        Candidate.board_instance == board.instance,
        Candidate.board_id == board.board_id,
        Candidate.state.in_(_CACHE_STATES),
        or_(Candidate.revisit_at.is_(None), Candidate.revisit_at > now),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _discovery_cache_domain(
    session: Session,
    domain: str,
    *,
    now: datetime,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    stmt = select(Candidate).where(
        Candidate.primary_domain == domain,
        Candidate.state.in_(_CACHE_STATES),
        or_(Candidate.revisit_at.is_(None), Candidate.revisit_at > now),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _candidate_for_employer_board(
    session: Session,
    board: EmployerBoard,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    live = _live_by_board(
        session,
        BoardKey(board.system, board.instance, board.board_id),
        exclude_id=exclude_id,
    )
    if live is not None:
        return live
    return _candidate_for_employer_id(session, board.employer_id, exclude_id=exclude_id)


def _candidate_for_employer(
    session: Session,
    employer: Employer,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    return _candidate_for_employer_id(session, employer.id, exclude_id=exclude_id)


def _candidate_for_employer_id(
    session: Session,
    employer_id: uuid.UUID,
    *,
    exclude_id: uuid.UUID | None,
) -> Candidate | None:
    stmt = select(Candidate).where(
        Candidate.employer_id == employer_id,
        Candidate.state.notin_(_LIVE),
    )
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return session.scalars(stmt).first()


def _enrich(
    survivor: Candidate,
    *,
    company_name: str,
    domains: list[str],
    primary: str | None,
    board: BoardKey | None,
) -> None:
    if not survivor.company_name and company_name:
        survivor.company_name = company_name
    if domains:
        merged = list(dict.fromkeys([*(survivor.domain_candidates or []), *domains]))
        survivor.domain_candidates = merged
    if survivor.primary_domain is None and primary is not None:
        survivor.primary_domain = primary
    if board is not None and survivor.board_id is None:
        survivor.board_system = board.system
        survivor.board_instance = board.instance
        survivor.board_id = board.board_id


def _merge_into(session: Session, *, donor: Candidate, survivor: Candidate) -> None:
    if donor.id == survivor.id:
        if donor.state == CandidateState.new:
            donor.state = CandidateState.queued
            donor.next_attempt_at = donor.next_attempt_at or datetime.now(UTC)
        return
    for row in session.scalars(
        select(CandidateEvidence).where(CandidateEvidence.candidate_id == donor.id)
    ):
        row.candidate_id = survivor.id
    donor.state = CandidateState.duplicate
    donor.merged_into_candidate_id = survivor.id
    donor.claimed_at = None
    donor.next_attempt_at = None
    _enrich(
        survivor,
        company_name=donor.company_name,
        domains=list(donor.domain_candidates or []),
        primary=donor.primary_domain,
        board=(
            BoardKey(donor.board_system, donor.board_instance, donor.board_id)
            if donor.board_system and donor.board_instance and donor.board_id
            else None
        ),
    )
    if survivor.state == CandidateState.new:
        survivor.state = CandidateState.queued
        survivor.next_attempt_at = survivor.next_attempt_at or datetime.now(UTC)
    session.flush()


def _add_evidence(
    session: Session,
    candidate: Candidate,
    *,
    search_result_id: uuid.UUID | None,
    quoted_text: str,
    source_url: str,
) -> None:
    session.add(
        CandidateEvidence(
            candidate_id=candidate.id,
            search_result_id=search_result_id,
            quoted_text=quoted_text[:200],
            source_url=source_url,
        )
    )
