"""The overall monthly money cap (provider "*"), as the admin budget page reads and sets it.

Setting a cap never edits an old row: the current one is ended and a new one starts now, so the
policy table keeps the history of what the cap was and when.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BudgetPolicy, SpendLedger

ALL_PROVIDERS = "*"
CURRENCY = "GBP"
MAX_MONTHLY_CAP = Decimal(100000)


class CapError(ValueError):
    """A cap the admin typed that can't be saved, with a message to show them."""


def _active(now: datetime):
    return (
        BudgetPolicy.period == "month",
        BudgetPolicy.effective_from <= now,
        (BudgetPolicy.effective_to.is_(None)) | (BudgetPolicy.effective_to > now),
    )


def active_caps(session: Session, now: datetime) -> list[BudgetPolicy]:
    """Every monthly cap in force now, the overall one and any for a single provider."""
    return list(
        session.scalars(select(BudgetPolicy).where(*_active(now)).order_by(BudgetPolicy.provider, BudgetPolicy.effective_from)).all()
    )


def overall_cap(session: Session, now: datetime) -> Decimal | None:
    rows = [row for row in active_caps(session, now) if row.provider == ALL_PROVIDERS and row.money_cap is not None]
    return Decimal(rows[-1].money_cap) if rows else None


def spent_this_month(session: Session, now: datetime) -> Decimal:
    """What the budget gate counts against the cap: settled costs, or estimates still held."""
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    cost = func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)
    return Decimal(session.scalar(select(func.coalesce(func.sum(cost), 0)).where(SpendLedger.occurred_at >= month_start)) or 0)


def parse_cap(raw: str) -> Decimal:
    text = raw.strip().lstrip("£").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise CapError("Enter the cap as a number of pounds, such as 25 or 12.50.") from exc
    if not value.is_finite() or value < 0:
        raise CapError("The cap must be 0 or more.")
    if value > MAX_MONTHLY_CAP:
        raise CapError(f"The cap can't be more than £{MAX_MONTHLY_CAP:,}.")
    if value != value.quantize(Decimal("0.01")):
        raise CapError("Use whole pence: at most two decimal places.")
    return value


def set_overall_cap(session: Session, amount: Decimal, now: datetime) -> BudgetPolicy:
    """End the overall caps in force and start a new one now."""
    for row in active_caps(session, now):
        if row.provider == ALL_PROVIDERS:
            row.effective_to = now
    row = BudgetPolicy(
        provider=ALL_PROVIDERS,
        period="month",
        money_cap=amount,
        request_cap=None,
        currency=CURRENCY,
        effective_from=now,
        effective_to=None,
    )
    session.add(row)
    session.flush()
    return row
