"""Admin applicant create/edit + T2 + salary isolation from search/budget."""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from app.applicants.discovery import to_discovery_preference
from app.applicants.salary import job_excluded_for_salary, salary_match_status
from app.applicants.search_view import build_role_location_query
from app.applicants.service import (
    create_applicant,
    get_applicant_with_preference,
    parse_preference_input,
    update_applicant,
)
from app.applicants.types import DiscoveryPreference, ExpectedSalary
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import SalaryStatus

ADMIN = "test-admin-key-step4"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(session: Session, admin_env: None) -> AsyncIterator[httpx.AsyncClient]:
    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_requires_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/applicants")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_without_salary_keeps_nulls(
    client: httpx.AsyncClient, session: Session
) -> None:
    response = await client.post(
        "/admin/applicants/new",
        data={
            "admin_key": ADMIN,
            "email": "nosalary@example.com",
            "display_name": "",
            "target_roles": "Data Engineer",
            "locations": "London",
            "sector": "tech",
            "salary_amount": "",
            "salary_currency": "GBP",
            "salary_period": "year",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    from sqlalchemy import select
    from app.models import Applicant

    applicant = session.scalars(
        select(Applicant).where(Applicant.email == "nosalary@example.com")
    ).one()
    _, pref = get_applicant_with_preference(session, applicant.id)  # type: ignore[misc]
    assert pref.salary_amount is None
    assert pref.salary_currency is None
    assert pref.salary_period is None


@pytest.mark.asyncio
async def test_create_and_edit_applicant(client: httpx.AsyncClient, session: Session) -> None:
    create = await client.post(
        "/admin/applicants/new",
        data={
            "admin_key": ADMIN,
            "email": "tester@example.com",
            "display_name": "Tester",
            "target_roles": "Backend Engineer\nPlatform Engineer",
            "locations": "Manchester\nRemote UK",
            "sector": "tech",
            "salary_amount": "75000",
            "salary_currency": "GBP",
            "salary_period": "year",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    from sqlalchemy import select
    from app.models import Applicant

    applicant = session.scalars(select(Applicant)).one()
    pair = get_applicant_with_preference(session, applicant.id)
    assert pair is not None
    _, pref = pair
    assert pref.target_roles == ["Backend Engineer", "Platform Engineer"]
    assert pref.locations == ["Manchester", "Remote UK"]
    assert pref.sector == "tech"
    assert pref.salary_amount == Decimal("75000")
    assert pref.salary_currency == "GBP"
    assert pref.salary_period == "year"

    edit = await client.post(
        f"/admin/applicants/{applicant.id}/edit",
        data={
            "admin_key": ADMIN,
            "email": "tester@example.com",
            "display_name": "Tester",
            "target_roles": "Backend Engineer",
            "locations": "Leeds",
            "sector": "tech",
            "salary_amount": "",
            "salary_currency": "",
            "salary_period": "year",
        },
        follow_redirects=False,
    )
    assert edit.status_code == 303
    session.refresh(pref)
    assert pref.target_roles == ["Backend Engineer"]
    assert pref.locations == ["Leeds"]
    assert pref.salary_amount is None
    assert pref.salary_currency is None
    assert pref.salary_period is None


def test_t2_no_salary_never_excludes_job() -> None:
    """T2: with no expected salary, no job is excluded for salary."""
    for known, minimum, maximum in (
        (True, Decimal("10000"), Decimal("20000")),
        (True, Decimal("200000"), None),
        (False, None, None),
        (True, None, None),
    ):
        status = salary_match_status(
            None,
            job_salary_known=known,
            job_min=minimum,
            job_max=maximum,
            job_currency="GBP",
            job_period="year",
        )
        assert status is SalaryStatus.unknown
        assert job_excluded_for_salary(status) is False


def test_salary_status_meets_and_below() -> None:
    expected = ExpectedSalary(amount=Decimal("70000"), currency="GBP", period="year")
    assert (
        salary_match_status(
            expected,
            job_salary_known=True,
            job_min=Decimal("60000"),
            job_max=Decimal("80000"),
            job_currency="GBP",
            job_period="year",
        )
        is SalaryStatus.meets
    )
    assert (
        salary_match_status(
            expected,
            job_salary_known=True,
            job_min=Decimal("40000"),
            job_max=Decimal("50000"),
            job_currency="GBP",
            job_period="year",
        )
        is SalaryStatus.below
    )
    assert job_excluded_for_salary(SalaryStatus.below) is True
    assert job_excluded_for_salary(SalaryStatus.meets) is False


def test_discovery_preference_has_no_salary_fields(session: Session) -> None:
    pref_in = parse_preference_input(
        target_roles=["Backend"],
        locations=["London"],
        salary_amount=Decimal("90000"),
        salary_currency="GBP",
        salary_period="year",
    )
    applicant = create_applicant(
        session, email="iso@example.com", preference=pref_in, display_name="Iso"
    )
    session.flush()
    _, row = get_applicant_with_preference(session, applicant.id)  # type: ignore[misc]
    discovery = to_discovery_preference(row)
    assert not hasattr(discovery, "salary_amount")
    assert not hasattr(discovery, "salary_currency")
    assert not hasattr(discovery, "salary_period")
    assert not hasattr(discovery, "expected_salary")
    query = build_role_location_query(discovery)
    assert "90000" not in query
    assert "GBP" not in query
    assert "Backend" in query


def test_budget_and_fetch_modules_do_not_read_applicant_salary() -> None:
    """Salary must not be readable by search/budget code paths (D3)."""
    root = Path(__file__).resolve().parents[1] / "app"
    forbidden_names = {"salary_amount", "salary_currency", "salary_period", "ExpectedSalary"}
    scanned: list[Path] = []
    for package in ("budget", "fetch"):
        for path in (root / package).rglob("*.py"):
            scanned.append(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden_names:
                    pytest.fail(f"{path} references {node.id}")
                if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                    pytest.fail(f"{path} references attribute {node.attr}")
    assert scanned, "expected budget/fetch modules to exist"

    # BudgetGate.reserve signature must not accept preference salary.
    from app.budget.gate import BudgetGate, SpendEstimate

    reserve_params = inspect.signature(BudgetGate.reserve).parameters
    assert "salary" not in reserve_params
    assert "expected_salary" not in reserve_params
    estimate_fields = set(SpendEstimate.__dataclass_fields__)
    assert "salary_amount" not in estimate_fields
    assert DiscoveryPreference.__dataclass_fields__.keys() == {
        "target_roles",
        "locations",
        "sector",
    }


def test_update_clears_salary(session: Session) -> None:
    pref = parse_preference_input(
        target_roles=["SRE"],
        locations=["Bristol"],
        salary_amount=Decimal("1"),
        salary_currency="GBP",
        salary_period="hour",
    )
    applicant = create_applicant(session, email="clear@example.com", preference=pref)
    session.flush()
    cleared = parse_preference_input(target_roles=["SRE"], locations=["Bristol"])
    update_applicant(session, applicant.id, preference=cleared)
    session.flush()
    _, row = get_applicant_with_preference(session, applicant.id)  # type: ignore[misc]
    assert row.salary_amount is None
    assert row.salary_currency is None
    assert row.salary_period is None
