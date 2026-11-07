"""Domain shapes for the candidate queue (MVP §9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from app.models import Candidate, CandidateState

# D12: verification retries after 1 h, 6 h, 24 h; then needs_review.
VERIFICATION_RETRY_DELAYS: tuple[timedelta, ...] = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)

CLAIM_STALE_AFTER = timedelta(minutes=15)

DedupeReason = Literal[
    "fresh",
    "board_directory",
    "board_live",
    "domain_directory",
    "domain_live",
    "name_group",
    "discovery_cache",
    "cf3_target",
]

RetryTerminal = Literal["retry_wait", "needs_review"]


@dataclass(frozen=True, slots=True)
class BoardKey:
    system: str
    instance: str
    board_id: str


@dataclass(frozen=True, slots=True)
class DedupeResult:
    """Outcome of the four-step dedupe before expensive checks."""

    survivor: Candidate
    created: bool
    reason: DedupeReason

    @property
    def state(self) -> str:
        return self.survivor.state


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    state: RetryTerminal
    attempt_count: int
    delay: timedelta | None
    next_attempt_at_offset: timedelta | None


def next_retry_schedule(attempt_count: int) -> RetrySchedule:
    """Map a failed verification attempt to retry_wait or needs_review.

    Mirrors search-run retries: delays apply while ``attempt_count`` is within
    the three D12 intervals; past that → ``needs_review`` (never unsupported/rejected).
    """
    if attempt_count < 1:
        raise ValueError("attempt_count must be >= 1 after a claimed verification")
    if attempt_count <= len(VERIFICATION_RETRY_DELAYS):
        delay = VERIFICATION_RETRY_DELAYS[attempt_count - 1]
        return RetrySchedule(
            state=CandidateState.retry_wait,
            attempt_count=attempt_count,
            delay=delay,
            next_attempt_at_offset=delay,
        )
    return RetrySchedule(
        state=CandidateState.needs_review,
        attempt_count=attempt_count,
        delay=None,
        next_attempt_at_offset=None,
    )
