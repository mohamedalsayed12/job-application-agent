"""Verification failure → retry_wait schedule or needs_review (MVP §9 / D8 / D12)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Candidate, CandidateState
from app.queue.types import RetrySchedule, next_retry_schedule


def schedule_verification_failure(
    candidate: Candidate,
    *,
    error: str,
    now: datetime | None = None,
) -> RetrySchedule:
    """Apply D12 backoff after a failed verification request.

    Never transitions to ``unsupported`` or ``rejected`` (D8 / T16).
    Expects ``attempt_count`` already incremented by the claim step.
    """
    stamp = now or datetime.now(UTC)
    attempts = int(candidate.attempt_count or 0)
    plan = next_retry_schedule(attempts)
    candidate.state = plan.state
    candidate.last_error = error
    candidate.claimed_at = None
    if plan.state == CandidateState.retry_wait:
        assert plan.delay is not None
        candidate.next_attempt_at = stamp + plan.delay
    else:
        candidate.next_attempt_at = None
    return plan
