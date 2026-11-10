"""Orchestrate leftover AI extraction: cache, budget, call, ground, persist."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings, get_settings
from app.extraction.ai_cache import input_hash, lookup_cached
from app.extraction.ai_call import call_leftover_extraction
from app.extraction.ai_grounding import (
    ground_suggestions,
    parse_one_employer,
    parse_suggestions,
)
from app.extraction.ai_injection import scan_suspicious
from app.extraction.ai_prompt import PROMPT_VERSION, estimate_input_tokens
from app.extraction.ai_types import (
    AiBudgetWait,
    AiCallFailure,
    AiExtractionOutcome,
    AiInFlightWait,
    AiTokenCapWait,
    GroundedHit,
    LeftoverResult,
    UngroundedItem,
)
from app.extraction.leftovers import leftover_search_results
from app.fetch.gateway import FetchGateway
from app.models import (
    AiExtraction,
    Candidate,
    ExtractedBy,
    SearchResult,
    SpendKind,
)
from app.queue.agencies import is_recruitment_agency
from app.queue.dedupe import ingest_candidate

logger = logging.getLogger(__name__)

AiExtractResult = AiExtractionOutcome | AiBudgetWait | AiTokenCapWait | AiInFlightWait
_PENDING_OUTPUT: dict[str, Any] = {"status": "pending"}
# Crash after commit of the pending marker can leave a permanent in-flight row.
_PENDING_STEAL_AFTER = timedelta(minutes=15)


async def extract_ai_leftovers(
    session: Session,
    gateway: FetchGateway,
    results: Iterable[SearchResult],
    *,
    settings: Settings | None = None,
    related_run_id: uuid.UUID | None = None,
    persist: Callable[[], None] | None = None,
    cf3_target: Candidate | None = None,
) -> AiExtractResult:
    """Run AI only on leftovers. Cached inputs skip the paid call.

    ``cf3_target``: the candidate a CF3 search ran for; same-name grounded hits
    enrich it instead of becoming new rows (Step 10 R1).
    """
    cfg = settings or get_settings()
    leftovers = leftover_search_results(results)
    if not leftovers:
        return AiExtractionOutcome(
            grounded=(),
            ungrounded=(),
            suspicious_result_ids=frozenset(),
            input_hash="",
            model=cfg.resolved_ai_extraction_model(),
            prompt_version=PROMPT_VERSION,
            cached=True,
        )

    model = cfg.resolved_ai_extraction_model()
    suspicious = scan_suspicious(leftovers)
    chunks = _chunk_leftovers(leftovers, cfg)
    if not chunks:
        return AiTokenCapWait(cap="per_item")

    grounded: list[GroundedHit] = []
    ungrounded: list[UngroundedItem] = []
    prompt_tokens = 0
    completion_tokens = 0
    cost_total = Decimal("0")
    all_cached = True
    last_row_id: uuid.UUID | None = None
    last_digest = ""
    last_model = model

    for chunk in chunks:
        outcome = await _extract_chunk(
            session,
            gateway,
            chunk,
            suspicious=suspicious,
            model=model,
            settings=cfg,
            related_run_id=related_run_id,
            persist=persist,
            cf3_target=cf3_target,
        )
        if isinstance(outcome, (AiBudgetWait, AiTokenCapWait, AiInFlightWait)):
            return outcome
        grounded.extend(outcome.grounded)
        ungrounded.extend(outcome.ungrounded)
        prompt_tokens += outcome.prompt_tokens
        completion_tokens += outcome.completion_tokens
        if isinstance(outcome.cost_amount, Decimal):
            cost_total += outcome.cost_amount
        all_cached = all_cached and outcome.cached
        last_row_id = outcome.ai_extraction_id or last_row_id
        last_digest = outcome.input_hash or last_digest
        last_model = outcome.model or last_model

    return AiExtractionOutcome(
        grounded=tuple(grounded),
        ungrounded=tuple(ungrounded),
        suspicious_result_ids=suspicious,
        input_hash=last_digest,
        model=last_model,
        prompt_version=PROMPT_VERSION,
        cached=all_cached,
        ai_extraction_id=last_row_id,
        cost_amount=cost_total,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


async def _extract_chunk(
    session: Session,
    gateway: FetchGateway,
    leftovers: Sequence[LeftoverResult],
    *,
    suspicious: frozenset[uuid.UUID],
    model: str,
    settings: Settings,
    related_run_id: uuid.UUID | None,
    persist: Callable[[], None] | None,
    cf3_target: Candidate | None = None,
) -> AiExtractResult:
    digest = input_hash(leftovers, model=model, prompt_version=PROMPT_VERSION)
    estimated_in = estimate_input_tokens(leftovers)
    max_out = settings.ai_extraction_max_output_tokens
    estimated_total = estimated_in + max_out
    if estimated_total > settings.ai_extraction_per_item_token_cap:
        # Still honour a finished cache hit when over the live-call token cap.
        cached = lookup_cached(session, digest=digest, prompt_version=PROMPT_VERSION)
        if cached is not None and not _is_pending(cached):
            outcome = _outcome_from_cache(cached, leftovers, suspicious)
            _persist_grounded_if_needed(session, outcome, leftovers, cf3_target)
            return outcome
        return AiTokenCapWait(cap="per_item")
    used_today = _tokens_used_today(session)
    if used_today + estimated_total > settings.ai_extraction_daily_token_cap:
        cached = lookup_cached(session, digest=digest, prompt_version=PROMPT_VERSION)
        if cached is not None and not _is_pending(cached):
            outcome = _outcome_from_cache(cached, leftovers, suspicious)
            _persist_grounded_if_needed(session, outcome, leftovers, cf3_target)
            return outcome
        return AiTokenCapWait(cap="per_day")

    claimed = _take_claim(
        session,
        digest=digest,
        model=model,
        leftovers=leftovers,
        suspicious=suspicious,
        cf3_target=cf3_target,
    )
    if not isinstance(claimed, AiExtraction):
        return claimed
    claim = claimed

    gate = BudgetGate(session)
    cost = estimate_cost(
        provider=settings.ai_provider,
        kind=SpendKind.ai_extraction,
        units=Decimal("1"),
        settings=settings,
    )
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider=settings.ai_provider,
                kind=SpendKind.ai_extraction,
                units=Decimal("1"),
                unit_label="leftover_batch",
                estimated_cost=cost,
                related_run_id=related_run_id,
            )
        )
    except BudgetWait as wait:
        session.delete(claim)
        session.flush()
        return AiBudgetWait(cap=wait.cap)

    persist_fn = persist or session.flush
    persist_fn()

    call = await call_leftover_extraction(
        gateway,
        leftovers,
        settings=settings,
        max_output_tokens=max_out,
    )
    if isinstance(call, AiCallFailure):
        gate.settle(reservation, consumed=False)
        session.delete(claim)
        session.flush()
        logger.warning("%s leftover extraction failed: %s", settings.ai_provider, call.reason)
        return AiExtractionOutcome(
            grounded=(),
            ungrounded=(UngroundedItem(reason=f"provider:{call.reason}"),),
            suspicious_result_ids=frozenset(
                item for item in suspicious if any(item == row.id for row in leftovers)
            ),
            input_hash=digest,
            model=model,
            prompt_version=PROMPT_VERSION,
            cached=False,
            cost_amount=Decimal("0"),
        )

    suggestions, parse_rejects = parse_suggestions(call.payload)
    grounded, ground_rejects = ground_suggestions(suggestions, leftovers)
    ungrounded = parse_rejects + ground_rejects

    # B4: settle the reserved per-batch unit price (tokens stay in output).
    gate.settle(reservation, actual_cost=cost, units=Decimal("1"))

    chunk_suspicious = frozenset(
        item for item in suspicious if any(item == row.id for row in leftovers)
    )
    claim.model = call.model or model
    claim.output = _serialize_output(
        grounded=grounded,
        ungrounded=ungrounded,
        suspicious=chunk_suspicious,
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
        provider_model=call.model,
    )
    claim.grounded_count = len(grounded)
    claim.ungrounded_count = len(ungrounded)
    claim.cost_amount = cost
    session.flush()

    outcome = AiExtractionOutcome(
        grounded=tuple(grounded),
        ungrounded=tuple(ungrounded),
        suspicious_result_ids=chunk_suspicious,
        input_hash=digest,
        model=claim.model,
        prompt_version=PROMPT_VERSION,
        cached=False,
        ai_extraction_id=claim.id,
        cost_amount=cost,
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
    )
    _persist_grounded(session, outcome, leftovers, cf3_target)
    return outcome


def _chunk_leftovers(
    leftovers: Sequence[LeftoverResult],
    settings: Settings,
) -> list[list[LeftoverResult]]:
    """Split so each provider call stays under the per-item token cap."""
    max_out = settings.ai_extraction_max_output_tokens
    cap = settings.ai_extraction_per_item_token_cap
    if max_out >= cap:
        return []

    chunks: list[list[LeftoverResult]] = []
    current: list[LeftoverResult] = []
    for row in leftovers:
        trial = current + [row]
        if estimate_input_tokens(trial) + max_out <= cap:
            current = trial
            continue
        if current:
            chunks.append(current)
            current = []
        if estimate_input_tokens([row]) + max_out <= cap:
            current = [row]
        else:
            # Single leftover cannot fit; stop chunking progress for this batch.
            return chunks if chunks else []
    if current:
        chunks.append(current)
    return chunks


def _is_pending(row: AiExtraction) -> bool:
    return isinstance(row.output, dict) and row.output.get("status") == "pending"


def _pending_is_stale(row: AiExtraction, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created <= moment - _PENDING_STEAL_AFTER


def _new_pending_claim(*, digest: str, model: str) -> AiExtraction:
    return AiExtraction(
        input_hash=digest,
        model=model,
        prompt_version=PROMPT_VERSION,
        output=dict(_PENDING_OUTPUT),
        grounded_count=0,
        ungrounded_count=0,
        cost_amount=Decimal("0"),
    )


def _resolve_cached_row(
    session: Session,
    row: AiExtraction,
    leftovers: Sequence[LeftoverResult],
    suspicious: frozenset[uuid.UUID],
    cf3_target: Candidate | None,
) -> AiExtractionOutcome | AiInFlightWait | None:
    """Return a finished outcome, an in-flight wait, or None after deleting a stale pending row."""
    if _is_pending(row):
        if not _pending_is_stale(row):
            return AiInFlightWait()
        # Lock before delete so a second pass waits instead of deleting a live claim.
        locked = session.scalars(
            select(AiExtraction)
            .where(AiExtraction.id == row.id)
            .with_for_update(skip_locked=True)
        ).first()
        if locked is None:
            return AiInFlightWait()
        if not _is_pending(locked):
            outcome = _outcome_from_cache(locked, leftovers, suspicious)
            _persist_grounded_if_needed(session, outcome, leftovers, cf3_target)
            return outcome
        if not _pending_is_stale(locked):
            return AiInFlightWait()
        session.delete(locked)
        session.flush()
        return None
    outcome = _outcome_from_cache(row, leftovers, suspicious)
    _persist_grounded_if_needed(session, outcome, leftovers, cf3_target)
    return outcome


def _take_claim(
    session: Session,
    *,
    digest: str,
    model: str,
    leftovers: Sequence[LeftoverResult],
    suspicious: frozenset[uuid.UUID],
    cf3_target: Candidate | None,
) -> AiExtraction | AiExtractionOutcome | AiInFlightWait:
    """Claim the cache slot for a live call, or return a cache hit / in-flight wait.

    Survives persist=commit. A stale pending row is deleted and the insert is
    retried once so a crash cannot block the hash forever.
    """
    existing = lookup_cached(session, digest=digest, prompt_version=PROMPT_VERSION)
    if existing is not None:
        resolved = _resolve_cached_row(
            session, existing, leftovers, suspicious, cf3_target
        )
        if resolved is not None:
            return resolved

    for _ in range(2):
        claim = _new_pending_claim(digest=digest, model=model)
        try:
            with session.begin_nested():
                session.add(claim)
                session.flush()
            return claim
        except IntegrityError:
            raced = lookup_cached(session, digest=digest, prompt_version=PROMPT_VERSION)
            if raced is None:
                return AiInFlightWait()
            resolved = _resolve_cached_row(
                session, raced, leftovers, suspicious, cf3_target
            )
            if resolved is not None:
                return resolved
    return AiInFlightWait()


def _tokens_used_today(session: Session, *, now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    day_start = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
    rows = session.scalars(
        select(AiExtraction).where(AiExtraction.created_at >= day_start)
    ).all()
    total = 0
    for row in rows:
        usage = row.output.get("usage") if isinstance(row.output, dict) else None
        if not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        if isinstance(prompt, int):
            total += prompt
        if isinstance(completion, int):
            total += completion
    return total


def _serialize_output(
    *,
    grounded: Sequence[GroundedHit],
    ungrounded: Sequence[UngroundedItem],
    suspicious: frozenset[uuid.UUID],
    prompt_tokens: int,
    completion_tokens: int,
    provider_model: str,
) -> dict[str, Any]:
    return {
        "employers": [
            {
                "company_name": hit.company_name,
                "domain_candidates": list(hit.domain_candidates),
                "evidence": [
                    {"result_id": str(ev.result_id), "quote": ev.quote}
                    for ev in hit.evidence
                ],
            }
            for hit in grounded
        ],
        "ungrounded": [
            {
                "reason": item.reason,
                "company_name": item.company_name,
                "detail": item.detail,
            }
            for item in ungrounded
        ],
        "suspicious_result_ids": [str(item) for item in sorted(suspicious, key=str)],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": provider_model,
        },
    }


def _outcome_from_cache(
    row: AiExtraction,
    leftovers: Sequence[LeftoverResult],
    suspicious: frozenset[uuid.UUID],
) -> AiExtractionOutcome:
    """Hydrate a typed outcome from a cached row without re-grounding."""
    output = row.output if isinstance(row.output, dict) else {}
    leftover_ids = {item.id for item in leftovers}
    grounded: list[GroundedHit] = []
    for entry in output.get("employers", []) if isinstance(output.get("employers"), list) else []:
        hit = _hit_from_cached_employer(entry, leftover_ids)
        if hit is not None:
            grounded.append(hit)
    ungrounded: list[UngroundedItem] = []
    stored_ungrounded = output.get("ungrounded")
    if isinstance(stored_ungrounded, list):
        for item in stored_ungrounded:
            if not isinstance(item, dict):
                continue
            reason = item.get("reason")
            if not isinstance(reason, str):
                continue
            ungrounded.append(
                UngroundedItem(
                    reason=reason,
                    company_name=item.get("company_name")
                    if isinstance(item.get("company_name"), str)
                    else None,
                    detail=item.get("detail")
                    if isinstance(item.get("detail"), str)
                    else None,
                )
            )
    merged_suspicious = set(suspicious)
    stored_suspicious = output.get("suspicious_result_ids")
    if isinstance(stored_suspicious, list):
        for value in stored_suspicious:
            if isinstance(value, str):
                try:
                    merged_suspicious.add(uuid.UUID(value))
                except ValueError:
                    continue
    usage = output.get("usage") if isinstance(output.get("usage"), dict) else {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    return AiExtractionOutcome(
        grounded=tuple(grounded),
        ungrounded=tuple(ungrounded),
        suspicious_result_ids=frozenset(merged_suspicious),
        input_hash=row.input_hash,
        model=row.model,
        prompt_version=row.prompt_version,
        cached=True,
        ai_extraction_id=row.id,
        cost_amount=Decimal(row.cost_amount)
        if row.cost_amount is not None
        else None,
        prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else 0,
        completion_tokens=completion_tokens
        if isinstance(completion_tokens, int)
        else 0,
    )


def _hit_from_cached_employer(
    entry: object,
    leftover_ids: set[uuid.UUID],
) -> GroundedHit | None:
    parsed = parse_one_employer(entry)
    if isinstance(parsed, UngroundedItem):
        return None
    name = parsed.company_name.strip()
    if not name:
        return None
    if not parsed.evidence or any(
        item.result_id not in leftover_ids for item in parsed.evidence
    ):
        return None
    return GroundedHit(
        company_name=name,
        domain_candidates=parsed.domain_candidates,
        evidence=parsed.evidence,
    )


def _persist_grounded_if_needed(
    session: Session,
    outcome: AiExtractionOutcome,
    leftovers: Sequence[LeftoverResult],
    cf3_target: Candidate | None = None,
) -> None:
    if outcome.ai_extraction_id is None:
        return
    existing = session.scalars(
        select(Candidate).where(Candidate.ai_extraction_id == outcome.ai_extraction_id)
    ).first()
    if existing is None:
        _persist_grounded(session, outcome, leftovers, cf3_target)


def _persist_grounded(
    session: Session,
    outcome: AiExtractionOutcome,
    leftovers: Sequence[LeftoverResult],
    cf3_target: Candidate | None = None,
) -> list[Candidate]:
    urls = {row.id: row.url for row in leftovers}
    touched: list[Candidate] = []
    seen: set[uuid.UUID] = set()
    for hit in outcome.grounded:
        if is_recruitment_agency(hit.company_name):
            continue
        evidence = [
            (item.result_id, item.quote, urls.get(item.result_id, ""))
            for item in hit.evidence
        ]
        result = ingest_candidate(
            session,
            company_name=hit.company_name,
            extracted_by=ExtractedBy.ai,
            domain_candidates=list(hit.domain_candidates),
            ai_extraction_id=outcome.ai_extraction_id,
            evidence=evidence,
            cf3_target=cf3_target,
        )
        if result.survivor.id not in seen:
            seen.add(result.survivor.id)
            touched.append(result.survivor)
    return touched
