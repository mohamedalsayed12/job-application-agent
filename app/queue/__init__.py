"""Candidate queue: dedupe, claim, and verification retry scheduling (Step 8)."""

from app.queue.claim import claim_next_candidate, release_stale_claims
from app.queue.dedupe import finalize_new_candidate, ingest_candidate
from app.queue.domains import normalize_domain
from app.queue.retry import schedule_verification_failure
from app.queue.types import (
    VERIFICATION_RETRY_DELAYS,
    BoardKey,
    DedupeResult,
    RetrySchedule,
    next_retry_schedule,
)

__all__ = [
    "VERIFICATION_RETRY_DELAYS",
    "BoardKey",
    "DedupeResult",
    "RetrySchedule",
    "claim_next_candidate",
    "finalize_new_candidate",
    "ingest_candidate",
    "next_retry_schedule",
    "normalize_domain",
    "release_stale_claims",
    "schedule_verification_failure",
]
