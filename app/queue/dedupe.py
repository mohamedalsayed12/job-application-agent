"""Four-step candidate dedupe before any expensive verification (MVP §9)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    Candidate,
    CandidateEvidence,
    CandidateState,
    Employer,
    EmployerBoard,
)
from app.queue.domains import normalize_domain
from app.queue.names import clean_company_name, same_company
from app.queue.types import BoardKey, DedupeReason, DedupeResult

# Partial-unique indexes ignore duplicate/rejected; live queue lookups also
# skip unsupported so discovery-cache (step 4) owns those rows.
_EXCLUDED_FROM_LIVE = tuple(CANDIDATE_LIVE_UNIQUE_EXCLUDES)
_CACHE_STATES = (CandidateState.unsupported, CandidateState.rejected)
_NOT_ACTIVE = _EXCLUDED_FROM_LIVE + (CandidateState.unsupported,)
_CLAIMABLE = (CandidateState.new, CandidateState.queued, CandidateState.retry_wait, CandidateState.verifying)
_DIRECTORY_REASONS = frozenset({"board_directory", "domain_directory"})


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
    cf3_target: Candidate | None = None,
) -> DedupeResult:
    """Insert or merge a candidate through the four-step dedupe; leave queued/duplicate.

    ``evidence`` items are ``(search_result_id, quoted_text, source_url)``.
    ``cf3_target`` is the candidate a CF3 search ran for: a same-name hit with
    domains enriches it instead of becoming a new row (Step 10 R1).
    """
    stamp = now or datetime.now(UTC)
    domains = list(
        dict.fromkeys(d for d in (normalize_domain(x) for x in (domain_candidates or [])) if d)
    )
    primary = normalize_domain(primary_domain)
    if primary is None and len(domains) == 1:
        primary = domains[0]
    cleaned = cleaned_name or clean_company_name(company_name)
    board = (
        BoardKey(board_system, board_instance, board_id)
        if board_system and board_instance and board_id
        else None
    )

    hit = _resolve(
        session,
        cleaned=cleaned,
        primary=primary,
        domains=domains,
        board=board,
        now=stamp,
        cf3_target=cf3_target,
    )
    if hit.kind == "none":
        survivor = _new_row(
            company_name=company_name,
            cleaned=cleaned,
            domains=domains,
            primary=primary,
            board=board,
            state=CandidateState.queued,
            extracted_by=extracted_by,
            ai_extraction_id=ai_extraction_id,
            next_attempt_at=stamp,
        )
        session.add(survivor)
        session.flush()
        result = DedupeResult(survivor=survivor, created=True, reason="fresh")
    elif hit.kind == "directory_only":
        survivor = _new_row(
            company_name=company_name,
            cleaned=cleaned,
            domains=domains,
            primary=primary,
            board=board,
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
        if hit.kind == "requeue_cache":
            survivor.state = CandidateState.queued
            survivor.attempt_count = 0
            survivor.claimed_at = None
            survivor.last_error = None
            survivor.next_attempt_at = stamp
            survivor.revisit_at = None
        _enrich(survivor, company_name=company_name, domains=domains, primary=primary, board=board)
        if ai_extraction_id is not None and survivor.ai_extraction_id is None:
            survivor.ai_extraction_id = ai_extraction_id
        if hit.reason in _DIRECTORY_REASONS:
            if hit.employer_id is not None:
                survivor.employer_id = hit.employer_id
            if survivor.state in _CLAIMABLE:
                survivor.state = CandidateState.duplicate
                survivor.claimed_at = None
                survivor.next_attempt_at = None
        elif survivor.state == CandidateState.new:
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


@dataclass(frozen=True, slots=True)
class _ResolveHit:
    kind: Literal["none", "existing", "directory_only", "requeue_cache"]
    reason: DedupeReason
    candidate: Candidate | None = None
    employer_id: uuid.UUID | None = None


def _resolve(
    session: Session,
    *,
    cleaned: str,
    primary: str | None,
    domains: list[str],
    board: BoardKey | None,
    now: datetime,
    cf3_target: Candidate | None = None,
) -> _ResolveHit:
    domain_keys = list(dict.fromkeys([*( [primary] if primary else [] ), *domains]))

    # 1. Board key — directory, live, discovery cache (active or due for requeue).
    if board is not None:
        directory = _board_in_directory(session, board)
        if directory is not None:
            linked = _any_for_board(session, board) or _any_for_employer(
                session, directory.employer_id
            )
            if linked is not None:
                return _ResolveHit(
                    "existing",
                    "board_directory",
                    candidate=linked,
                    employer_id=directory.employer_id,
                )
            return _ResolveHit(
                "directory_only",
                "board_directory",
                employer_id=directory.employer_id,
            )
        live = _live_by_board(session, board)
        if live is not None:
            return _ResolveHit("existing", "board_live", candidate=live)
        board_match = (
            Candidate.board_system == board.system,
            Candidate.board_instance == board.instance,
            Candidate.board_id == board.board_id,
        )
        hit = _discovery_cache_hit(session, *board_match, now=now)
        if hit is not None:
            return hit

    # 2. Official / primary domain — try each domain key (primary + candidates).
    for domain in domain_keys:
        employer = session.scalars(
            select(Employer).where(Employer.official_domain == domain)
        ).first()
        if employer is not None:
            linked = (
                _live_by_domain(session, domain)
                or _live_by_domain_candidate(session, domain)
                or _any_for_employer(session, employer.id)
            )
            if linked is not None:
                return _ResolveHit(
                    "existing",
                    "domain_directory",
                    candidate=linked,
                    employer_id=employer.id,
                )
            return _ResolveHit(
                "directory_only",
                "domain_directory",
                employer_id=employer.id,
            )
        live = _live_by_domain(session, domain) or _live_by_domain_candidate(
            session, domain
        )
        if live is not None:
            return _ResolveHit("existing", "domain_live", candidate=live)
        hit = _discovery_cache_hit(
            session,
            or_(
                Candidate.primary_domain == domain,
                _array_has(Candidate.domain_candidates, domain),
            ),
            now=now,
        )
        if hit is not None:
            return hit

    # 2b. CF3 target — the search ran for this row, so a same-name hit is its
    #     domain evidence. Only while the target still has no domain of its own.
    if _accepts_cf3_hit(cf3_target, cleaned=cleaned, board=board):
        return _ResolveHit("existing", "cf3_target", candidate=cf3_target)

    # 3. Cleaned name — group only when both sides have no board and no domains.
    #    A matching name alone never merges across different domains.
    if primary is None and board is None and not domains:
        hit = _discovery_cache_hit(
            session,
            Candidate.cleaned_name == cleaned,
            Candidate.primary_domain.is_(None),
            Candidate.board_system.is_(None),
            func.cardinality(Candidate.domain_candidates) == 0,
            now=now,
        )
        if hit is not None:
            return hit
        grouped = _live_by_name_bare(session, cleaned)
        if grouped is not None:
            return _ResolveHit("existing", "name_group", candidate=grouped)

    return _ResolveHit("none", "fresh")


def _accepts_cf3_hit(
    target: Candidate | None, *, cleaned: str, board: BoardKey | None
) -> bool:
    if target is None or board is not None:
        return False
    if target.state in CANDIDATE_LIVE_UNIQUE_EXCLUDES:
        return False
    if target.primary_domain or target.domain_candidates:
        return False
    return same_company(cleaned, target.cleaned_name)


def _new_row(
    *,
    company_name: str,
    cleaned: str,
    domains: list[str],
    primary: str | None,
    board: BoardKey | None,
    state: str,
    extracted_by: str,
    ai_extraction_id: uuid.UUID | None = None,
    next_attempt_at: datetime | None = None,
    employer_id: uuid.UUID | None = None,
) -> Candidate:
    return Candidate(
        company_name=company_name,
        cleaned_name=cleaned,
        domain_candidates=domains,
        primary_domain=primary,
        board_system=board.system if board else None,
        board_instance=board.instance if board else None,
        board_id=board.board_id if board else None,
        state=state,
        extracted_by=extracted_by,
        ai_extraction_id=ai_extraction_id,
        next_attempt_at=next_attempt_at,
        employer_id=employer_id,
    )


def _board_in_directory(session: Session, board: BoardKey) -> EmployerBoard | None:
    return session.scalars(
        select(EmployerBoard).where(
            EmployerBoard.system == board.system,
            EmployerBoard.instance == board.instance,
            EmployerBoard.board_id == board.board_id,
        )
    ).first()


def _live_by_board(session: Session, board: BoardKey) -> Candidate | None:
    return session.scalars(
        select(Candidate).where(
            Candidate.board_system == board.system,
            Candidate.board_instance == board.instance,
            Candidate.board_id == board.board_id,
            Candidate.state.notin_(_NOT_ACTIVE),
        )
    ).first()


def _live_by_domain(session: Session, domain: str) -> Candidate | None:
    return session.scalars(
        select(Candidate).where(
            Candidate.primary_domain == domain,
            Candidate.state.notin_(_NOT_ACTIVE),
        )
    ).first()


def _array_has(column: ColumnElement[list[str]], value: str):
    """True when a text[] column contains ``value`` (Postgres ``@>``)."""
    return column.contains([value])


def _live_by_domain_candidate(session: Session, domain: str) -> Candidate | None:
    """Match a host listed in domain_candidates when primary_domain is unset."""
    return session.scalars(
        select(Candidate).where(
            Candidate.primary_domain.is_(None),
            _array_has(Candidate.domain_candidates, domain),
            Candidate.state.notin_(_NOT_ACTIVE),
        )
    ).first()


def _live_by_name_bare(session: Session, cleaned: str) -> Candidate | None:
    """Same cleaned name with no board and empty domain list — one queue row (T13)."""
    return session.scalars(
        select(Candidate).where(
            Candidate.cleaned_name == cleaned,
            Candidate.primary_domain.is_(None),
            Candidate.board_system.is_(None),
            func.cardinality(Candidate.domain_candidates) == 0,
            Candidate.state.notin_(_NOT_ACTIVE),
        )
    ).first()


def _any_for_board(session: Session, board: BoardKey) -> Candidate | None:
    """Reuse any non-rejected row for this board, including ``duplicate`` shells."""
    return session.scalars(
        select(Candidate).where(
            Candidate.board_system == board.system,
            Candidate.board_instance == board.instance,
            Candidate.board_id == board.board_id,
            Candidate.state != CandidateState.rejected,
        )
    ).first()


def _any_for_employer(session: Session, employer_id: uuid.UUID) -> Candidate | None:
    return session.scalars(
        select(Candidate).where(
            Candidate.employer_id == employer_id,
            Candidate.state != CandidateState.rejected,
        )
    ).first()


def _active_cache_predicate(now: datetime):
    """Rejected never retries; unsupported blocks until revisit_at is due."""
    return or_(
        Candidate.state == CandidateState.rejected,
        and_(
            Candidate.state == CandidateState.unsupported,
            or_(Candidate.revisit_at.is_(None), Candidate.revisit_at > now),
        ),
    )


def _due_unsupported_predicate(now: datetime):
    return and_(
        Candidate.state == CandidateState.unsupported,
        Candidate.revisit_at.is_not(None),
        Candidate.revisit_at <= now,
    )


def _cache_lookup(
    session: Session,
    *key_filters: ColumnElement[bool],
    now: datetime,
    due: bool,
) -> Candidate | None:
    predicate = _due_unsupported_predicate(now) if due else _active_cache_predicate(now)
    return session.scalars(select(Candidate).where(*key_filters, predicate)).first()


def _discovery_cache_hit(
    session: Session,
    *key_filters: ColumnElement[bool],
    now: datetime,
) -> _ResolveHit | None:
    cached = _cache_lookup(session, *key_filters, now=now, due=False)
    if cached is not None:
        return _ResolveHit("existing", "discovery_cache", candidate=cached)
    due = _cache_lookup(session, *key_filters, now=now, due=True)
    if due is not None:
        return _ResolveHit("requeue_cache", "discovery_cache", candidate=due)
    return None


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
