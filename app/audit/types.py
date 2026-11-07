"""Domain shapes for the Step 10 classification audit sheet (D21 / T28 accuracy half)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.models import CandidateState

# Terminal classification outcomes the auditor judges. Never includes rejected
# (reviewer-only) or registered (post-directory).
CLASSIFIED_STATES: Final[frozenset[str]] = frozenset(
    {
        CandidateState.verified,
        CandidateState.needs_review,
        CandidateState.unsupported,
    }
)

DEFAULT_SAMPLE_SIZE: Final[int] = 20

# CSV columns the operator fills; system never writes auditor_* / is_* / notes.
OPERATOR_FILL_COLUMNS: Final[tuple[str, ...]] = (
    "auditor_result",
    "is_correct",
    "is_useful",
    "notes",
)

CSV_COLUMNS: Final[tuple[str, ...]] = (
    "audit_sample_id",
    "candidate_id",
    "company_name",
    "cleaned_name",
    "primary_domain",
    "board_system",
    "board_instance",
    "board_id",
    "system_result",
    "evidence_urls",
    *OPERATOR_FILL_COLUMNS,
)


@dataclass(frozen=True, slots=True)
class AuditSheetRow:
    """One sampled company for the operator CSV. Auditor fields stay empty."""

    audit_sample_id: UUID
    candidate_id: UUID
    company_name: str
    cleaned_name: str
    primary_domain: str | None
    board_system: str | None
    board_instance: str | None
    board_id: str | None
    system_result: str
    evidence_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """Counts after sampling. Used by CLI/admin; never judges correctness."""

    requested: int
    selected: int
    by_state: dict[str, int]
    classified_available: dict[str, int]
