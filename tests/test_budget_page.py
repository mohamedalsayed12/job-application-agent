"""The admin budget page: the monthly cap can be read and set without touching the database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.policy import CapError, overall_cap, parse_cap, set_overall_cap
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import BudgetPolicy, SpendKind, SpendLedger

ADMIN = "test-admin-key-budget-page"
HEADERS = {"X-Admin-Key": ADMIN}


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = lambda: session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _estimate(cost: str) -> SpendEstimate:
    return SpendEstimate(
        provider="apify",
        kind=SpendKind.linkedin_jobs,
        estimated_cost=Decimal(cost),
        units=Decimal(1),
        unit_label="run",
    )


async def test_the_budget_page_needs_the_admin_key(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/budget")).status_code == 404
    assert (await client.post("/admin/budget", data={"money_cap": "10"})).status_code == 404


async def test_with_no_cap_the_pages_say_paid_calls_are_waiting(client: httpx.AsyncClient) -> None:
    budget = await client.get("/admin/budget", headers=HEADERS)
    assert budget.status_code == 200
    assert "No monthly budget cap is set" in budget.text

    quality = await client.get("/admin/quality", headers=HEADERS)
    assert "No monthly budget cap is set" in quality.text
    assert 'href="/admin/budget"' in quality.text


async def test_setting_the_cap_lets_paid_calls_through(client: httpx.AsyncClient, session: Session) -> None:
    with pytest.raises(BudgetWait):
        BudgetGate(session).reserve(_estimate("1.00"))

    saved = await client.post("/admin/budget", data={"money_cap": "£25.50"}, headers=HEADERS)
    assert saved.status_code == 303

    assert overall_cap(session, datetime.now(UTC)) == Decimal("25.50")
    BudgetGate(session).reserve(_estimate("1.00"))
    page = await client.get("/admin/budget?saved=1", headers=HEADERS)
    assert "Spent £1.00 of £25.50 this month" in page.text
    assert "No monthly budget cap is set" not in (await client.get("/admin/quality", headers=HEADERS)).text


async def test_changing_the_cap_keeps_the_old_one_as_history(client: httpx.AsyncClient, session: Session) -> None:
    await client.post("/admin/budget", data={"money_cap": "10"}, headers=HEADERS)
    await client.post("/admin/budget", data={"money_cap": "40"}, headers=HEADERS)

    rows = session.scalars(select(BudgetPolicy).where(BudgetPolicy.provider == "*").order_by(BudgetPolicy.effective_from)).all()
    assert [row.money_cap for row in rows] == [Decimal(10), Decimal(40)]
    assert rows[0].effective_to is not None and rows[1].effective_to is None
    assert overall_cap(session, datetime.now(UTC)) == Decimal(40)


async def test_a_zero_cap_pauses_paid_calls(client: httpx.AsyncClient, session: Session) -> None:
    await client.post("/admin/budget", data={"money_cap": "0"}, headers=HEADERS)
    assert "every paid call is paused" in (await client.get("/admin/budget", headers=HEADERS)).text
    with pytest.raises(BudgetWait):
        BudgetGate(session).reserve(_estimate("0.10"))


@pytest.mark.parametrize("typed", ["", "abc", "-5", "1.234", "999999999", "NaN"])
async def test_a_bad_cap_is_refused_with_a_message(client: httpx.AsyncClient, session: Session, typed: str) -> None:
    response = await client.post("/admin/budget", data={"money_cap": typed}, headers=HEADERS)
    assert response.status_code in (400, 422)
    assert session.scalar(select(BudgetPolicy)) is None


def test_parse_cap_reads_pounds() -> None:
    assert parse_cap(" £1,250.00 ") == Decimal("1250.00")
    with pytest.raises(CapError):
        parse_cap("Infinity")


def test_the_cap_counts_spend_from_this_month_only(session: Session) -> None:
    now = datetime.now(UTC)
    set_overall_cap(session, Decimal(5), now)
    session.add(
        SpendLedger(
            provider="apify",
            kind=SpendKind.linkedin_jobs,
            estimated_cost=Decimal(100),
            actual_cost=Decimal(100),
            units=Decimal(1),
            unit_label="run",
            occurred_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
    )
    session.flush()
    BudgetGate(session).reserve(_estimate("4.00"))
