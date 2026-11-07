"""Stratified audit sampling across verified / needs_review / unsupported.

Never fills auditor_result, is_correct, is_useful, or notes (operator only).
One open sheet: if unfilled audit_sample rows already exist, re-export them
instead of inserting another draw.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.types import (
    CLASSIFIED_STATES,
    DEFAULT_SAMPLE_SIZE,
    MAX_SAMPLE_SIZE,
    AuditSheetRow,
    SampleSummary,
)
from app.models import AuditSample, Candidate, CandidateEvidence, VerificationCheck


class InsufficientClassifiedError(Exception):
    """Fewer eligible classified companies than the requested sample size."""

    def __init__(self, available: int, requested: int) -> None:
        self.available = available
        self.requested = requested
        super().__init__(
            f"need at least {requested} classified companies, found {available}"
        )


def _validate_size(size: int) -> None:
    if size < 1 or size > MAX_SAMPLE_SIZE:
        raise ValueError(f"size must be 1–{MAX_SAMPLE_SIZE}")


def round_robin_take(
    buckets: dict[str, Sequence[object]],
    *,
    size: int,
    order: Sequence[str] = CLASSIFIED_STATES,
) -> list[object]:
    """Take up to ``size`` items, cycling state buckets in ``order``."""
    picked: list[object] = []
    for group in zip_longest(*(buckets.get(state, ()) for state in order)):
        for row in group:
            if row is None:
                continue
            picked.append(row)
            if len(picked) >= size:
                return picked
    return picked


def _already_sampled_ids(session: Session) -> set[UUID]:
    return set(session.scalars(select(AuditSample.candidate_id)).all()) - {None}


def list_classified_candidates(session: Session) -> list[Candidate]:
    """Classified candidates not yet on an audit sheet, oldest first."""
    sampled = _already_sampled_ids(session)
    rows = session.scalars(
        select(Candidate)
        .where(Candidate.state.in_(CLASSIFIED_STATES))
        .order_by(Candidate.created_at, Candidate.id)
    ).all()
    return [row for row in rows if row.id not in sampled]


def stratified_sample(
    candidates: Sequence[Candidate],
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
) -> list[Candidate]:
    """Pick ``size`` companies, spreading across the three states."""
    _validate_size(size)
    buckets: dict[str, list[Candidate]] = {state: [] for state in CLASSIFIED_STATES}
    for candidate in candidates:
        if candidate.state in buckets:
            buckets[candidate.state].append(candidate)

    available = sum(len(bucket) for bucket in buckets.values())
    if available < size:
        raise InsufficientClassifiedError(available, size)

    picked = round_robin_take(buckets, size=size)
    return [row for row in picked if isinstance(row, Candidate)]


def _evidence_by_candidate(
    session: Session, candidate_ids: Sequence[UUID]
) -> dict[UUID, tuple[str, ...]]:
    """Batch-load unique http(s) evidence URLs per candidate, stable order."""
    if not candidate_ids:
        return {}
    ordered: dict[UUID, list[str]] = {cid: [] for cid in candidate_ids}
    seen: dict[UUID, set[str]] = {cid: set() for cid in candidate_ids}

    def _add(cid: UUID, url: str | None) -> None:
        if not url:
            return
        cleaned = url.strip()
        if not cleaned or cleaned in seen[cid]:
            return
        lower = cleaned.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            return
        seen[cid].add(cleaned)
        ordered[cid].append(cleaned)

    for row in session.scalars(
        select(CandidateEvidence)
        .where(CandidateEvidence.candidate_id.in_(candidate_ids))
        .order_by(CandidateEvidence.created_at, CandidateEvidence.id)
    ):
        _add(row.candidate_id, row.source_url)

    for row in session.scalars(
        select(VerificationCheck)
        .where(VerificationCheck.candidate_id.in_(candidate_ids))
        .order_by(VerificationCheck.checked_at, VerificationCheck.id)
    ):
        _add(row.candidate_id, row.evidence_url)

    return {cid: tuple(urls) for cid, urls in ordered.items()}


def _sheet_rows_for(
    session: Session,
    pairs: Sequence[tuple[AuditSample, Candidate]],
    *,
    requested: int,
    available: dict[str, int] | None = None,
) -> tuple[list[AuditSheetRow], SampleSummary]:
    evidence = _evidence_by_candidate(session, [c.id for _, c in pairs])
    rows: list[AuditSheetRow] = []
    by_state: dict[str, int] = defaultdict(int)
    for sample, candidate in pairs:
        by_state[candidate.state] += 1
        rows.append(
            AuditSheetRow(
                audit_sample_id=sample.id,
                candidate_id=candidate.id,
                company_name=candidate.company_name,
                cleaned_name=candidate.cleaned_name,
                primary_domain=candidate.primary_domain,
                board_system=candidate.board_system,
                board_instance=candidate.board_instance,
                board_id=candidate.board_id,
                system_result=sample.system_result,
                evidence_urls=evidence.get(candidate.id, ()),
            )
        )
    avail = available or {state: 0 for state in CLASSIFIED_STATES}
    summary = SampleSummary(
        requested=requested,
        selected=len(rows),
        by_state={str(state): count for state, count in by_state.items()},
        classified_available={str(state): avail.get(state, 0) for state in CLASSIFIED_STATES},
    )
    return rows, summary


def _open_sheet_pairs(session: Session) -> list[tuple[AuditSample, Candidate]]:
    """Unfilled audit_sample rows joined to their candidates, oldest first."""
    rows = session.execute(
        select(AuditSample, Candidate)
        .join(Candidate, Candidate.id == AuditSample.candidate_id)
        .where(AuditSample.auditor_result.is_(None))
        .where(AuditSample.candidate_id.is_not(None))
        .order_by(AuditSample.created_at, AuditSample.id)
    ).all()
    return [(sample, candidate) for sample, candidate in rows]


def create_audit_sheet(
    session: Session,
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[list[AuditSheetRow], SampleSummary]:
    """Return the open audit sheet, creating one stratified draw if none exists.

    Auditor judgment columns stay NULL on insert. Does not commit.
    """
    _validate_size(size)
    open_pairs = _open_sheet_pairs(session)
    if open_pairs:
        return _sheet_rows_for(session, open_pairs, requested=size)

    classified = list_classified_candidates(session)
    available_by: dict[str, int] = {state: 0 for state in CLASSIFIED_STATES}
    for candidate in classified:
        available_by[candidate.state] = available_by.get(candidate.state, 0) + 1

    selected = stratified_sample(classified, size=size)
    stamp = datetime.now(tz=UTC)
    samples: list[AuditSample] = []
    for index, candidate in enumerate(selected):
        sample = AuditSample(
            candidate_id=candidate.id,
            employer_id=candidate.employer_id,
            system_result=candidate.state,
            auditor_result=None,
            is_correct=None,
            is_useful=None,
            notes=None,
            created_at=stamp + timedelta(microseconds=index),
        )
        session.add(sample)
        samples.append(sample)
    session.flush()
    pairs = list(zip(samples, selected, strict=True))
    return _sheet_rows_for(session, pairs, requested=size, available=available_by)
