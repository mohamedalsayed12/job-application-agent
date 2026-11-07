"""Claim queued / retry_wait candidates for verification (SKIP LOCKED)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CANDIDATE_CLAIMABLE, Candidate, CandidateState
from app.queue.types import CLAIM_STALE_AFTER


def claim_next_candidate(
    session: Session,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Candidate | None:
    """Claim one due ``queued`` / ``retry_wait`` row into ``verifying``.

    Commits are the caller's job (budget gate / worker). Tests flush only.
    """
    stamp = now or (clock() if clock else datetime.now(UTC))
    _requeue_due_unsupported(session, now=stamp)
    release_stale_claims(session, now=stamp)

    due = session.scalars(
        select(Candidate)
        .where(
            Candidate.state.in_(tuple(CANDIDATE_CLAIMABLE)),
            Candidate.next_attempt_at.is_not(None),
            Candidate.next_attempt_at <= stamp,
            # Step 10 R2: no domain yet → wait for the CF3 search before verifying.
            or_(
                Candidate.primary_domain.is_not(None),
                func.cardinality(Candidate.domain_candidates) > 0,
                Candidate.domain_searched_at.is_not(None),
            ),
        )
        .order_by(Candidate.next_attempt_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if due is None:
        return None

    due.state = CandidateState.verifying
    due.claimed_at = stamp
    due.attempt_count = int(due.attempt_count or 0) + 1
    due.last_error = None
    session.flush()
    return due


def release_stale_claims(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Return stuck ``verifying`` rows to ``retry_wait`` without bumping attempts."""
    stamp = now or datetime.now(UTC)
    cutoff = stamp - CLAIM_STALE_AFTER
    stale = session.scalars(
        select(Candidate).where(
            Candidate.state == CandidateState.verifying,
            Candidate.claimed_at.is_not(None),
            Candidate.claimed_at < cutoff,
        )
    ).all()
    for row in stale:
        row.state = CandidateState.retry_wait
        row.claimed_at = None
        row.next_attempt_at = stamp
        row.last_error = "stale_claim_recovered"
    if stale:
        session.flush()
    return len(stale)


def _requeue_due_unsupported(session: Session, *, now: datetime) -> None:
    """unsupported → queued when revisit_at is reached (D13)."""
    due = session.scalars(
        select(Candidate).where(
            Candidate.state == CandidateState.unsupported,
            Candidate.revisit_at.is_not(None),
            Candidate.revisit_at <= now,
        )
    ).all()
    for row in due:
        row.state = CandidateState.queued
        row.next_attempt_at = now
        row.claimed_at = None
        row.attempt_count = 0
        row.last_error = None
    if due:
        session.flush()
