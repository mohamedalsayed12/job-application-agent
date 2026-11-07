"""Stratified audit sampling across verified / needs_review / unsupported.

Never fills auditor_result, is_correct, is_useful, or notes (operator only).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.evidence import evidence_urls_for
from app.audit.types import (
    CLASSIFIED_STATES,
    DEFAULT_SAMPLE_SIZE,
    AuditSheetRow,
    SampleSummary,
)
from app.models import AuditSample, Candidate, CandidateState

_STATE_ORDER: tuple[str, ...] = (
    CandidateState.verified,
    CandidateState.needs_review,
    CandidateState.unsupported,
)


class AuditSampleError(Exception):
    """Base error for audit sampling."""


class InsufficientClassifiedError(AuditSampleError):
    """Fewer classified companies than the requested sample size."""

    def __init__(self, available: int, requested: int) -> None:
        self.available = available
        self.requested = requested
        super().__init__(
            f"need at least {requested} classified companies, found {available}"
        )


def list_classified_candidates(session: Session) -> list[Candidate]:
    """All candidates in CLASSIFIED_STATES, oldest first for stable sampling."""
    rows = session.scalars(
        select(Candidate)
        .where(Candidate.state.in_(CLASSIFIED_STATES))
        .order_by(Candidate.created_at, Candidate.id)
    ).all()
    return list(rows)


def stratified_sample(
    candidates: Sequence[Candidate],
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
) -> list[Candidate]:
    """Pick up to ``size`` companies, spreading across the three states.

    Round-robin from each non-empty bucket (verified → needs_review →
    unsupported), then fill leftovers from remaining pools in state order.
    Deterministic given the input order.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    buckets: dict[str, list[Candidate]] = {state: [] for state in _STATE_ORDER}
    for candidate in candidates:
        if candidate.state in buckets:
            buckets[candidate.state].append(candidate)

    available = sum(len(bucket) for bucket in buckets.values())
    if available < size:
        raise InsufficientClassifiedError(available, size)

    picked: list[Candidate] = []
    indices = {state: 0 for state in _STATE_ORDER}
    while len(picked) < size:
        progressed = False
        for state in _STATE_ORDER:
            if len(picked) >= size:
                break
            bucket = buckets[state]
            i = indices[state]
            if i < len(bucket):
                picked.append(bucket[i])
                indices[state] = i + 1
                progressed = True
        if not progressed:
            break
    return picked


def create_audit_sheet(
    session: Session,
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[list[AuditSheetRow], SampleSummary]:
    """Insert ``audit_sample`` rows for a stratified sample and return sheet rows.

    Auditor judgment columns stay NULL. Does not commit — caller commits.
    """
    classified = list_classified_candidates(session)
    available_by: dict[str, int] = defaultdict(int)
    for candidate in classified:
        available_by[candidate.state] += 1

    selected = stratified_sample(classified, size=size)
    rows: list[AuditSheetRow] = []
    by_state: dict[str, int] = defaultdict(int)

    for candidate in selected:
        sample = AuditSample(
            candidate_id=candidate.id,
            employer_id=candidate.employer_id,
            system_result=candidate.state,
            auditor_result=None,
            is_correct=None,
            is_useful=None,
            notes=None,
        )
        session.add(sample)
        session.flush()
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
                system_result=candidate.state,
                evidence_urls=evidence_urls_for(session, candidate.id),
            )
        )

    summary = SampleSummary(
        requested=size,
        selected=len(rows),
        by_state={str(state): count for state, count in by_state.items()},
        classified_available={
            str(state): available_by[state] for state in _STATE_ORDER
        },
    )
    return rows, summary
