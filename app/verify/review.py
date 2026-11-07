"""Apply reviewer decisions on needs_review candidates (MVP §9)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candidate, CandidateState, ReviewDecision, ReviewItem
from app.verify.types import UNSUPPORTED_REVISIT


class ReviewError(Exception):
    """Review action rejected."""


class ReviewNotFoundError(ReviewError):
    pass


class ReviewConflictError(ReviewError):
    pass


def list_open_reviews(session: Session) -> list[tuple[ReviewItem, Candidate]]:
    rows = session.execute(
        select(ReviewItem, Candidate)
        .join(Candidate, Candidate.id == ReviewItem.candidate_id)
        .where(ReviewItem.decided_at.is_(None))
        .order_by(ReviewItem.priority.asc(), ReviewItem.created_at.asc())
    ).all()
    return [(item, candidate) for item, candidate in rows]


def apply_review_decision(
    session: Session,
    review_item_id,
    *,
    decision: ReviewDecision | str,
    reason: str,
    reviewer: str = "admin",
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ReviewItem:
    """Approve, reject, or mark unsupported. Saves the reason on the review row."""
    stamp = now or (clock() if clock else datetime.now(UTC))
    notes = reason.strip()
    if not notes:
        raise ReviewConflictError("reason is required")

    if isinstance(decision, str):
        try:
            decision = ReviewDecision(decision)
        except ValueError as exc:
            raise ReviewConflictError(f"invalid decision: {decision}") from exc

    item = session.get(ReviewItem, review_item_id)
    if item is None:
        raise ReviewNotFoundError("review item not found")
    if item.decided_at is not None:
        raise ReviewConflictError("review item already decided")

    candidate = session.get(Candidate, item.candidate_id)
    if candidate is None:
        raise ReviewNotFoundError("candidate not found")
    if candidate.state != CandidateState.needs_review:
        raise ReviewConflictError(
            f"candidate is {candidate.state}, expected needs_review"
        )

    item.decision = decision
    item.notes = notes
    item.reviewer = reviewer
    item.decided_at = stamp

    candidate.claimed_at = None
    candidate.next_attempt_at = None
    candidate.last_error = None
    if decision == ReviewDecision.verified:
        candidate.state = CandidateState.verified
        candidate.revisit_at = None
    elif decision == ReviewDecision.rejected:
        candidate.state = CandidateState.rejected
        candidate.revisit_at = None
    else:
        candidate.state = CandidateState.unsupported
        candidate.revisit_at = stamp + UNSUPPORTED_REVISIT

    session.flush()
    return item
