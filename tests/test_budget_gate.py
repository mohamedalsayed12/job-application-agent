"""T3: budget gate refuses paid calls over caps; work stays non-failed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
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


def _paid(
    *,
    cost: Decimal,
    provider: str = "serper",
) -> SpendEstimate:
    return SpendEstimate(
        provider=provider,
        kind=SpendKind.ai_extraction,
        units=Decimal("1"),
        unit_label="token",
        estimated_cost=cost,
    )


def _search(
    *,
    cost: Decimal,
    run_id: uuid.UUID,
    provider: str = "serper",
    units: Decimal = Decimal("1"),
) -> SpendEstimate:
    return SpendEstimate(
        provider=provider,
        kind=SpendKind.search,
        units=units,
        unit_label="request",
        estimated_cost=cost,
        related_run_id=run_id,
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
    assert exc.value.cap == "monthly"


def test_t3_monthly_cap_refuses_and_leaves_no_ledger(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("10.00"), now)
    session.add(
        SpendLedger(
            provider="serper",
            kind=SpendKind.ai_extraction,
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


def test_t3_daily_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("30.00"), now)
    gate = _gate(session, now)
    gate.reserve(_paid(cost=Decimal("0.80")))
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_paid(cost=Decimal("0.30")))
    assert exc.value.cap == "daily"


def test_t3_per_run_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("1000.00"), now)
    _, run_id = _applicant_and_run(session)
    gate = _gate(session, now)
    for _ in range(10):
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run_id))
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run_id))
    assert exc.value.cap == "per_run"


def test_t3_per_applicant_cap_refuses(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("1000.00"), now)
    applicant_id, _ = _applicant_and_run(session)
    gate = _gate(session, now)
    for i in range(20):
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
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run.id))
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
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run.id))
    assert exc.value.cap == "per_applicant"


def test_t3_per_applicant_from_run_without_estimate_applicant_id(
    session: Session,
) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("1000.00"), now)
    applicant_id, _ = _applicant_and_run(session)
    gate = _gate(session, now)
    for i in range(20):
        run = SearchRun(
            template_id="CF1",
            applicant_id=applicant_id,
            query=f"q{i}",
            provider="serper",
            status=SearchRunStatus.planned,
        )
        session.add(run)
        session.flush()
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run.id))
    run = SearchRun(
        template_id="CF1",
        applicant_id=applicant_id,
        query="overflow",
        provider="serper",
        status=SearchRunStatus.planned,
    )
    session.add(run)
    session.flush()
    with pytest.raises(BudgetWait) as exc:
        gate.reserve(_search(cost=Decimal("0.01"), run_id=run.id))
    assert exc.value.cap == "per_applicant"


def test_t3_budget_wait_leaves_search_run_planned(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    _, run_id = _applicant_and_run(session)
    gate = _gate(session, now)
    with pytest.raises(BudgetWait):
        gate.reserve(_search(cost=Decimal("1.00"), run_id=run_id))
    run = session.get(SearchRun, run_id)
    assert run is not None
    assert run.status == SearchRunStatus.planned


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


def test_reed_zero_cost_allowed_when_money_cap_zero(session: Session) -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("0"), now)
    _, run_id = _applicant_and_run(session)
    gate = _gate(session, now)
    cost = estimate_cost(
        provider="reed",
        kind=SpendKind.search,
        units=Decimal("1"),
        settings=_settings(),
    )
    reservation = gate.reserve(_search(cost=cost, run_id=run_id, provider="reed"))
    assert reservation.estimated_cost == Decimal("0")
