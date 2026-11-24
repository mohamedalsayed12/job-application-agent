"""Budget gate: reserve before a paid call, settle after; BudgetWait never fails work.

Contract: commit the session that called ``reserve`` before the paid outside call
so the ledger hold survives a crash. ``settle`` may run in a later transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import BudgetPolicy, SpendKind, SpendLedger

logger = logging.getLogger(__name__)

# "unset": no money cap is configured, so paid work waits until someone sets one.
CapName = Literal["monthly", "unset"]
WARN_RATIO = Decimal("0.8")
# Fixed advisory key for all budget reserves (one worker / one gate).
_BUDGET_ADVISORY_LOCK_KEY = 0x4A414142  # "JAAB"


@dataclass(frozen=True, slots=True)
class SpendEstimate:
    provider: str
    kind: SpendKind
    units: Decimal
    unit_label: str
    estimated_cost: Decimal
    related_item_id: uuid.UUID | None = None
    applicant_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SpendReservation:
    ledger_id: uuid.UUID
    estimated_cost: Decimal


class BudgetWait(Exception):
    """Caller must leave work queued / planned. Never mark failed (T3)."""

    def __init__(self, cap: CapName) -> None:
        self.cap = cap
        super().__init__(f"budget wait: {cap}")


def _require_non_negative_cost(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{label} must be a Decimal")
    if value.is_nan() or value.is_infinite() or value < 0:
        raise ValueError(f"{label} must be a finite Decimal >= 0")
    return value


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _latest_money_cap(rows: list[BudgetPolicy]) -> Decimal | None:
    capped = [row for row in rows if row.money_cap is not None]
    if not capped:
        return None
    capped.sort(key=lambda row: row.effective_from, reverse=True)
    return Decimal(capped[0].money_cap)


def _monthly_money_caps(
    session: Session, provider: str, now: datetime
) -> tuple[Decimal | None, Decimal | None]:
    """Provider-specific cap and wildcard ``*`` cap (either may be absent)."""
    policies = session.scalars(
        select(BudgetPolicy).where(
            BudgetPolicy.period == "month",
            BudgetPolicy.effective_from <= now,
            (BudgetPolicy.effective_to.is_(None)) | (BudgetPolicy.effective_to > now),
            BudgetPolicy.provider.in_(("*", provider)),
        )
    ).all()
    specific = _latest_money_cap([row for row in policies if row.provider == provider])
    wildcard = _latest_money_cap([row for row in policies if row.provider == "*"])
    return specific, wildcard


def _sum_spend(session: Session, *, since: datetime, provider: str | None = None) -> Decimal:
    query = select(
        func.coalesce(
            func.sum(func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)),
            0,
        )
    ).where(SpendLedger.occurred_at >= since)
    if provider is not None:
        query = query.where(SpendLedger.provider == provider)
    total = session.scalar(query)
    return Decimal(total or 0)


def _warn_if_needed(spent: Decimal, cap: Decimal, label: CapName) -> None:
    if cap <= 0:
        return
    if spent >= cap * WARN_RATIO:
        logger.warning("spend crossed 80%% of %s cap (%s / %s)", label, spent, cap)


class BudgetGate:
    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    def reserve(self, estimate: SpendEstimate) -> SpendReservation:
        # Serialize cap checks until this transaction commits.
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _BUDGET_ADVISORY_LOCK_KEY},
        )
        cost = _require_non_negative_cost(estimate.estimated_cost, label="estimated_cost")
        units = _require_non_negative_cost(estimate.units, label="units")

        now = self._clock()
        specific_cap, wildcard_cap = _monthly_money_caps(self._session, estimate.provider, now)
        requires_money = cost > 0
        month_start = _month_start(now)

        if requires_money and specific_cap is None and wildcard_cap is None:
            raise BudgetWait("unset")
        if requires_money and specific_cap is not None and specific_cap <= 0:
            raise BudgetWait("monthly")
        if requires_money and specific_cap is None and wildcard_cap is not None and wildcard_cap <= 0:
            raise BudgetWait("monthly")

        if requires_money and specific_cap is not None:
            spent = _sum_spend(self._session, since=month_start, provider=estimate.provider)
            if spent + cost > specific_cap:
                raise BudgetWait("monthly")
            _warn_if_needed(spent + cost, specific_cap, "monthly")
        if requires_money and wildcard_cap is not None:
            spent = _sum_spend(self._session, since=month_start)
            if spent + cost > wildcard_cap:
                raise BudgetWait("monthly")
            _warn_if_needed(spent + cost, wildcard_cap, "monthly")

        row = SpendLedger(
            provider=estimate.provider,
            kind=estimate.kind,
            related_item_id=estimate.related_item_id,
            estimated_cost=cost,
            actual_cost=None,
            units=units,
            unit_label=estimate.unit_label,
            occurred_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return SpendReservation(ledger_id=row.id, estimated_cost=cost)

    def settle(
        self,
        reservation: SpendReservation,
        *,
        actual_cost: Decimal | None = None,
        units: Decimal | None = None,
        consumed: bool = True,
    ) -> None:
        row = self._session.get(SpendLedger, reservation.ledger_id)
        if row is None:
            raise KeyError(f"spend_ledger row missing: {reservation.ledger_id}")
        if not consumed:
            row.actual_cost = Decimal(0)
        elif actual_cost is not None:
            row.actual_cost = _require_non_negative_cost(actual_cost, label="actual_cost")
        else:
            row.actual_cost = reservation.estimated_cost
        if units is not None:
            row.units = _require_non_negative_cost(units, label="units")
        self._session.flush()
