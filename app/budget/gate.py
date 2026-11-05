"""Budget gate: reserve before a paid call, settle after; BudgetWait never fails work."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BudgetPolicy, SearchRun, SpendKind, SpendLedger

logger = logging.getLogger(__name__)

CapName = Literal["monthly", "daily", "per_run", "per_applicant"]
PER_RUN_SEARCH_CAP = 10
PER_APPLICANT_DAY_SEARCH_CAP = 20
WARN_RATIO = Decimal("0.8")


@dataclass(frozen=True, slots=True)
class SpendEstimate:
    provider: str
    kind: SpendKind
    units: Decimal
    unit_label: str
    estimated_cost: Decimal
    related_run_id: uuid.UUID | None = None
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


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _load_monthly_money_cap(session: Session, provider: str, now: datetime) -> Decimal | None:
    policies = session.scalars(
        select(BudgetPolicy).where(
            BudgetPolicy.period == "month",
            BudgetPolicy.effective_from <= now,
            (BudgetPolicy.effective_to.is_(None)) | (BudgetPolicy.effective_to > now),
            BudgetPolicy.provider.in_(("*", provider)),
        )
    ).all()
    if not policies:
        return None
    policies = sorted(
        policies,
        key=lambda row: (row.provider == provider, row.effective_from),
        reverse=True,
    )
    money_cap = policies[0].money_cap
    if money_cap is None:
        return None
    return Decimal(money_cap)


def _sum_spend(session: Session, *, since: datetime) -> Decimal:
    total = session.scalar(
        select(
            func.coalesce(
                func.sum(func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)),
                0,
            )
        ).where(SpendLedger.occurred_at >= since)
    )
    return Decimal(total or 0)


def _count_search_for_run(session: Session, run_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(SpendLedger)
            .where(
                SpendLedger.related_run_id == run_id,
                SpendLedger.kind == SpendKind.search,
            )
        )
        or 0
    )


def _count_search_for_applicant_day(
    session: Session,
    applicant_id: uuid.UUID,
    day_start: datetime,
) -> int:
    day_end = day_start + timedelta(days=1)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SpendLedger)
            .join(SearchRun, SearchRun.id == SpendLedger.related_run_id)
            .where(
                SearchRun.applicant_id == applicant_id,
                SpendLedger.kind == SpendKind.search,
                SpendLedger.occurred_at >= day_start,
                SpendLedger.occurred_at < day_end,
            )
        )
        or 0
    )


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
        now = self._clock()
        money_cap = _load_monthly_money_cap(self._session, estimate.provider, now)
        requires_money = estimate.estimated_cost > 0

        if requires_money and (money_cap is None or money_cap <= 0):
            raise BudgetWait("monthly")

        if requires_money and money_cap is not None:
            month_start = _month_start(now)
            day_start = _day_start(now)
            monthly_spent = _sum_spend(self._session, since=month_start)
            daily_spent = _sum_spend(self._session, since=day_start)
            daily_cap = (money_cap / Decimal(30)).quantize(Decimal("0.0001"))
            if monthly_spent + estimate.estimated_cost > money_cap:
                raise BudgetWait("monthly")
            if daily_spent + estimate.estimated_cost > daily_cap:
                raise BudgetWait("daily")
            _warn_if_needed(monthly_spent + estimate.estimated_cost, money_cap, "monthly")
            _warn_if_needed(daily_spent + estimate.estimated_cost, daily_cap, "daily")

        if estimate.kind == SpendKind.search:
            day_start = _day_start(now)
            if estimate.related_run_id is not None:
                run_count = _count_search_for_run(self._session, estimate.related_run_id)
                if run_count + 1 > PER_RUN_SEARCH_CAP:
                    raise BudgetWait("per_run")
            if estimate.applicant_id is not None:
                applicant_count = _count_search_for_applicant_day(
                    self._session,
                    estimate.applicant_id,
                    day_start,
                )
                if applicant_count + 1 > PER_APPLICANT_DAY_SEARCH_CAP:
                    raise BudgetWait("per_applicant")

        row = SpendLedger(
            provider=estimate.provider,
            kind=estimate.kind,
            related_run_id=estimate.related_run_id,
            related_item_id=estimate.related_item_id,
            estimated_cost=estimate.estimated_cost,
            actual_cost=None,
            units=estimate.units,
            unit_label=estimate.unit_label,
            occurred_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return SpendReservation(ledger_id=row.id, estimated_cost=estimate.estimated_cost)

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
            row.actual_cost = Decimal("0")
        elif actual_cost is not None:
            row.actual_cost = actual_cost
        else:
            row.actual_cost = reservation.estimated_cost
        if units is not None:
            row.units = units
        self._session.flush()
