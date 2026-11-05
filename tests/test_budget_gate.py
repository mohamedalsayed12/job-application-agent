"""T3: budget gate refuses paid calls over caps; work stays non-failed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate, SpendReservation
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.models import (
    Applicant,
    BudgetPolicy,
    SearchRun,
    SearchRunStatus,
    SearchTemplate,
    SearchTemplateStatus,
    SpendKind,
    SpendLedger,
)


def _settings() -> Settings:
    return Settings(
        search_request_unit_price=Decimal("1.00"),
        ai_extraction_unit_price=Decimal("0.50"),
        ai_summary_unit_price=Decimal("0.25"),
    )


def _gate(session: Session, now: datetime) -> BudgetGate:
    return BudgetGate(session, clock=lambda: now)


def _seed_template(session: Session) -> None:
    if session.get(SearchTemplate, "CF1") is None:
        session.add(
            SearchTemplate(
                id="CF1",
                search_type="company-first",
                provider="serper",
                pattern="test",
                status=SearchTemplateStatus.unproven,
            )
        )
        session.flush()


def _applicant_and_run(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    _seed_template(session)
    applicant = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(applicant)
    session.flush()
    run = SearchRun(
        template_id="CF1",
        applicant_id=applicant.id,
        query="engineer",
        provider="serper",
        status=SearchRunStatus.planned,
    )
    session.add(run)
    session.flush()
    return applicant.id, run.id


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


def _estimate(
    *,
    cost: Decimal,
    run_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID | None = None,
    provider: str = "serper",
    kind: SpendKind = SpendKind.search,
    units: Decimal = Decimal("1"),
) -> SpendEstimate:
    return SpendEstimate(
        provider=provider,
        kind=kind,
        units=units,
        unit_label="request",
        estimated_cost=cost,
        related_run_id=run_id,
        applicant_id=applicant_id,
    )


def test_t3_cap_zero_blocks_paid_calls(session: Session) -> None:
    from sqlalchemy import select

    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_estimate(cost=Decimal("1.00")))
    assert exc.value.cap == "monthly"
    assert list(session.scalars(select(SpendLedger))) == []


def test_t3_missing_policy_blocks_paid_calls(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_estimate(cost=Decimal("1.00")))
    assert exc.value.cap == "monthly"


def test_t3_monthly_cap_refuses_and_leaves_no_ledger(session: Session) -> None:
    from sqlalchemy import select

    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("10.00"), now)
    # Prior days in the month already used £9.50; today is empty so daily still allows £0.33+.
    session.add(
        SpendLedger(
            provider="serper",
            kind=SpendKind.search,
            estimated_cost=Decimal("9.50"),
            actual_cost=Decimal("9.50"),
            units=Decimal("1"),
            unit_label="request",
            occurred_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        )
    )
    session.flush()
    gate = _gate(session, now)
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_estimate(cost=Decimal("1.00")))
    assert exc.value.cap == "monthly"
    rows = list(session.scalars(select(SpendLedger)))
    assert len(rows) == 1
    assert rows[0].actual_cost == Decimal("9.50")


def test_t3_daily_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    # monthly 30 → daily 1.00
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    gate.reserve(_estimate(cost=Decimal("0.80")))
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_estimate(cost=Decimal("0.30")))
    assert exc.value.cap == "daily"


def test_t3_per_run_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("1000.00"), now)
    _, run_id = _applicant_and_run(session)
    gate = _gate(session, now)
    for _ in range(10):
        gate.reserve(_estimate(cost=Decimal("0.01"), run_id=run_id))
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_estimate(cost=Decimal("0.01"), run_id=run_id))
    assert exc.value.cap == "per_run"


def test_t3_per_applicant_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("1000.00"), now)
    applicant_id, _ = _applicant_and_run(session)
    gate = _gate(session, now)
    for i in range(20):
        # Separate runs so per-run cap is not hit first.
        _seed_template(session)
        run = SearchRun(
            template_id="CF1",
            applicant_id=applicant_id,
            query=f"q{i}",
            provider="serper",
            status=SearchRunStatus.planned,
        )
        session.add(run)
        session.flush()
        gate.reserve(
            _estimate(cost=Decimal("0.01"), run_id=run.id, applicant_id=applicant_id)
        )
    with pytest.raises(BudgetWait) as exc:
        run = SearchRun(
            template_id="CF1",
            applicant_id=applicant_id,
            query="overflow",
            provider="serper",
            status=SearchRunStatus.planned,
        )
        session.add(run)
        session.flush()
        gate.reserve(
            _estimate(cost=Decimal("0.01"), run_id=run.id, applicant_id=applicant_id)
        )
    assert exc.value.cap == "per_applicant"


def test_t3_budget_wait_is_not_a_failure_signal(session: Session) -> None:
    """Callers branch on BudgetWait and keep work queued (T3)."""
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    gate = _gate(session, now)
    work_status = "planned"
    try:
        gate.reserve(_estimate(cost=Decimal("1.00")))
        work_status = "running"
    except BudgetWait:
        pass
    assert work_status == "planned"


def test_reserve_then_settle_records_actual(session: Session) -> None:
    from sqlalchemy import select

    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    # monthly 30 → daily 1.00
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    reservation = gate.reserve(_estimate(cost=Decimal("0.50")))
    assert isinstance(reservation, SpendReservation)
    gate.settle(reservation, actual_cost=Decimal("0.40"))
    row = session.scalars(select(SpendLedger)).one()
    assert row.estimated_cost == Decimal("0.50")
    assert row.actual_cost == Decimal("0.40")


def test_reed_zero_cost_allowed_when_money_cap_zero(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    gate = _gate(session, now)
    cost = estimate_cost(
        provider="reed",
        kind=SpendKind.search,
        units=Decimal("1"),
        settings=_settings(),
    )
    reservation = gate.reserve(_estimate(cost=cost, provider="reed"))
    assert reservation.estimated_cost == Decimal("0")
