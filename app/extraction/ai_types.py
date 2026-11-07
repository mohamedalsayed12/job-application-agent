"""Domain shapes for leftover AI extraction (Stage 3 / Step 7 / MVP §10)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class LeftoverResult:
    """One ``search_result`` row code extraction could not handle."""

    id: uuid.UUID
    url: str
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class AiEvidenceItem:
    result_id: uuid.UUID
    quote: str


@dataclass(frozen=True, slots=True)
class AiSuggestion:
    """Fixed AI output shape before grounding (no board IDs)."""

    company_name: str
    domain_candidates: tuple[str, ...]
    evidence: tuple[AiEvidenceItem, ...]


@dataclass(frozen=True, slots=True)
class UngroundedItem:
    """Rejected suggestion or field; counted toward the ungrounded metric."""

    reason: str
    company_name: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class GroundedHit:
    """Suggestion that passed every §10.4 check."""

    company_name: str
    domain_candidates: tuple[str, ...]
    evidence: tuple[AiEvidenceItem, ...]


@dataclass(frozen=True, slots=True)
class AiBudgetWait:
    """Money or request cap blocked the call; work waits (T3 / §11)."""

    cap: str


@dataclass(frozen=True, slots=True)
class AiTokenCapWait:
    """Token cap blocked the call; work waits, never marked failed (§11)."""

    cap: str


@dataclass(frozen=True, slots=True)
class AiInFlightWait:
    """Another worker already claimed this input hash; retry later (§10.6)."""


@dataclass(frozen=True, slots=True)
class AiExtractionOutcome:
    """Result of one leftover batch (live call or cache hit)."""

    grounded: tuple[GroundedHit, ...]
    ungrounded: tuple[UngroundedItem, ...]
    suspicious_result_ids: frozenset[uuid.UUID]
    input_hash: str
    model: str
    prompt_version: str
    cached: bool
    ai_extraction_id: uuid.UUID | None = None
    cost_amount: Decimal | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_suggestions: tuple[AiSuggestion, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AiCallResponse:
    """Parsed structured output from one provider call (any AI_PROVIDER)."""

    payload: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass(frozen=True, slots=True)
class AiCallFailure:
    """Provider call did not yield usable JSON; ``reason`` is ``<provider>_<detail>``."""

    reason: str
