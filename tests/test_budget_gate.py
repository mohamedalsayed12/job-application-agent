"""T3: budget gate refuses paid calls over caps; work stays non-failed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import (
    BudgetGate,
    BudgetWait,
    SpendEstimate,
    SpendReservation,
)
from app.core.config import Settings
from app.models import (
    BudgetPolicy,
    SpendKind,
    SpendLedger,
)


def _settings() -> Settings:
    return Settings(
        ai_summary_unit_price=Decimal("0.25"),
    )


def _gate(session: Session, now: datetime) -> BudgetGate:
    return BudgetGate(session, clock=lambda: now)


def _policy(session: Session, money_cap: Decimal | None, now: datetime) -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=money_cap,
            request_cap=None,
            currency="GBP",
            effective_from=now,
            effective_to=None,
        )
    )
    session.flush()


def _paid(
    *,
    cost: Decimal,
    provider: str = "serper",
) -> SpendEstimate:
    return SpendEstimate(
        provider=provider,
        kind=SpendKind.ai_summary,
        units=Decimal("1"),
        unit_label="token",
        estimated_cost=cost,
    )


def test_t3_cap_zero_blocks_paid_calls(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_paid(cost=Decimal("1.00")))
    assert exc.value.cap == "monthly"
    assert list(session.scalars(select(SpendLedger))) == []


def test_t3_missing_policy_blocks_paid_calls(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_paid(cost=Decimal("1.00")))
    assert exc.value.cap == "unset"


def test_t3_monthly_cap_refuses_and_leaves_no_ledger(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("10.00"), now)
    session.add(
        SpendLedger(
            provider="serper",
            kind=SpendKind.ai_summary,
            estimated_cost=Decimal("9.50"),
            actual_cost=Decimal("9.50"),
            units=Decimal("1"),
            unit_label="token",
            occurred_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        )
    )
    session.flush()
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_paid(cost=Decimal("1.00")))
    assert exc.value.cap == "monthly"
    rows = list(session.scalars(select(SpendLedger)))
    assert len(rows) == 1
    assert rows[0].actual_cost == Decimal("9.50")


def test_a_provider_cap_only_counts_that_provider_s_spend(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    session.add(
        BudgetPolicy(
            provider="anthropic",
            period="month",
            money_cap=Decimal("10.00"),
            request_cap=None,
            currency="GBP",
            effective_from=now,
            effective_to=None,
        )
    )
    session.add(
        SpendLedger(
            provider="apify",
            kind=SpendKind.linkedin_jobs,
            estimated_cost=Decimal("9.50"),
            actual_cost=Decimal("9.50"),
            units=Decimal("1"),
            unit_label="job",
            occurred_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        )
    )
    session.flush()
    gate = _gate(session, now)
    gate.reserve(_paid(cost=Decimal("1.00"), provider="anthropic"))


def test_the_whole_monthly_cap_can_be_spent_in_one_day(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    gate.reserve(_paid(cost=Decimal("29.00")))
    gate.reserve(_paid(cost=Decimal("1.00")))
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_paid(cost=Decimal("0.01")))
    assert exc.value.cap == "monthly"


def test_negative_cost_rejected(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    with pytest.raises(ValueError):
        gate.reserve(_paid(cost=Decimal("-0.01")))
    assert list(session.scalars(select(SpendLedger))) == []


def test_reserve_then_settle_records_actual(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    reservation = gate.reserve(_paid(cost=Decimal("0.50")))
    assert isinstance(reservation, SpendReservation)
    gate.settle(reservation, actual_cost=Decimal("0.40"))
    row = session.scalars(select(SpendLedger)).one()
    assert row.estimated_cost == Decimal("0.50")
    assert row.actual_cost == Decimal("0.40")


@pytest.mark.parametrize("kind", list(SpendKind))
def test_every_spend_kind_can_be_recorded_after_all_migrations(session: Session, kind: SpendKind) -> None:
    # Each migration rewrites the kind check from its own list; a later one must not drop an earlier kind.
    session.add(
        SpendLedger(
            provider="test",
            kind=kind,
            estimated_cost=Decimal("0.01"),
            units=Decimal(1),
            unit_label="call",
            occurred_at=datetime(2026, 11, 16, tzinfo=UTC),
        )
    )
    session.flush()


def test_d72_the_newest_cap_set_from_the_command_line_is_the_one_shown(session: Session) -> None:
    from app.budget.__main__ import caps, set_cap

    now = datetime.now(UTC)
    set_cap(session, "apify", Decimal("5"), now=now - timedelta(days=1))
    set_cap(session, "apify", Decimal("15"), now=now)

    assert caps(session, now=now) == [("apify", Decimal("15"), Decimal("0"))]
