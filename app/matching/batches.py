"""Ask the AI about many jobs, a few at a time, inside the money cap and a day's token cap.

Each question is reserved against both caps before its batch is sent, so a batch never overspends;
the calls in a batch run at once, and their answers are recorded one at a time afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate, SpendReservation
from app.extraction.ai_anthropic import send_anthropic_request
from app.extraction.ai_injection import scan_suspicious
from app.extraction.ai_types import AiCallFailure, AiCallResponse, LeftoverResult
from app.fetch.gateway import FetchGateway
from app.models import SpendKind

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4
_PROMPT_OVERHEAD_TOKENS = 400


@dataclass(frozen=True, slots=True)
class Ask[T]:
    item: T
    job_id: uuid.UUID
    title: str
    url: str
    # Everything sent besides the instructions: scanned for injected instructions and used for the token estimate.
    text: str
    body: Callable[[], dict]


@dataclass(frozen=True, slots=True)
class Spend:
    kind: SpendKind
    unit_label: str
    cost: Decimal
    provider: str


type Record[T] = Callable[[Ask[T], AiCallResponse], int]


async def ask_in_batches[T](
    session: Session,
    asks: Iterable[Ask[T]],
    *,
    gateway: FetchGateway,
    api_key: str,
    model: str,
    spend: Spend,
    max_output_tokens: int,
    daily_token_cap: int,
    used_today: int,
    concurrency: int,
    outcomes: Counter[str],
    record: Record[T],
    persist: Callable[[], None],
    now: datetime,
) -> str | None:
    """Send every ask until a cap stops it. Returns what stopped it, or None when all were sent.

    `record` saves one answer and returns the tokens it used; failed calls are released, not charged.
    """
    gate = BudgetGate(session, clock=lambda: now)
    pending = iter(asks)
    used = used_today
    while True:
        batch: list[tuple[Ask[T], SpendReservation]] = []
        planned = 0
        stopped: str | None = None
        for ask in pending:
            if scan_suspicious([LeftoverResult(id=ask.job_id, url=ask.url, title=ask.title, snippet=ask.text)]):
                outcomes["suspicious"] += 1
                continue
            estimate = len(ask.text) // _CHARS_PER_TOKEN + _PROMPT_OVERHEAD_TOKENS + max_output_tokens
            if used + planned + estimate > daily_token_cap:
                stopped = "daily token cap"
                break
            try:
                reservation = gate.reserve(
                    SpendEstimate(
                        provider=spend.provider,
                        kind=spend.kind,
                        units=Decimal(1),
                        unit_label=spend.unit_label,
                        estimated_cost=spend.cost,
                        related_item_id=ask.job_id,
                    )
                )
            except BudgetWait:
                stopped = "budget"
                break
            planned += estimate
            batch.append((ask, reservation))
            if len(batch) >= max(1, concurrency):
                break
        if batch:
            persist()
            calls = await asyncio.gather(
                *(send_anthropic_request(gateway, ask.body(), api_key=api_key, fallback_model=model) for ask, _ in batch)
            )
            for (ask, reservation), call in zip(batch, calls, strict=True):
                if isinstance(call, AiCallFailure):
                    logger.warning("%s failed for job %s: %s", spend.unit_label, ask.job_id, call.reason)
                    gate.settle(reservation, consumed=False)
                    outcomes["failed"] += 1
                else:
                    gate.settle(reservation, actual_cost=spend.cost, units=Decimal(1))
                    used += record(ask, call)
                persist()
        if stopped or not batch:
            return stopped
