"""T5 / T6 / T9 and search_run states with mocked Mojeek and Reed responses."""

from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import (
    Applicant,
    BudgetPolicy,
    SearchResult,
    SearchRun,
    SearchRunStatus,
    SpendLedger,
    TemplateStats,
)
from app.search.planner import plan_cold_start
from app.search.runner import SearchBudgetWait, run_search
from app.search.schedule import create_runs_from_plan
from app.search.templates import (
    DISABLED_UNTIL_V9,
    ENABLED_TEMPLATE_IDS,
    TEMPLATE_SPECS,
    UNPROVEN_RUN_THRESHOLD,
    ensure_search_templates,
    is_template_enabled,
)


def _settings(**overrides: object) -> Settings:
    base = dict(
        web_search_provider="mojeek",
        mojeek_api_key="test-mojeek-key",
        reed_api_key="test-reed-key",
        adzuna_enabled=False,
        search_request_unit_price=Decimal("0.003"),
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _pref() -> DiscoveryPreference:
    return DiscoveryPreference(
        target_roles=("Software Engineer",),
        locations=("London",),
        sector="tech",
    )


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _policy(session: Session, money_cap: Decimal, now: datetime) -> None:
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


def _applicant(session: Session) -> Applicant:
    row = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(row)
    session.flush()
    return row


def _gateway(handler: httpx.MockTransport | object) -> FetchGateway:
    transport = (
        handler
        if isinstance(handler, httpx.MockTransport)
        else httpx.MockTransport(handler)  # type: ignore[arg-type]
    )
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.mojeek.com", "www.reed.co.uk", "api.reed.co.uk"}),
        resolve=_public_resolve,
        transport=transport,
    )


def test_templates_seeded_cf1b_disabled(session: Session) -> None:
    cfg = _settings()
    rows = ensure_search_templates(session, cfg)
    assert {r.id for r in rows} == {s.id for s in TEMPLATE_SPECS}
    assert "CF1b" in DISABLED_UNTIL_V9
    assert not is_template_enabled("CF1b", cfg)
    assert ENABLED_TEMPLATE_IDS == frozenset({"CF1", "CF2", "CF3", "BD1", "BD2"})
    by_id = {r.id: r for r in rows}
    assert by_id["CF1"].provider == "reed"
    assert by_id["CF2"].provider == "mojeek"
    assert by_id["BD1"].provider == "mojeek"


def test_t5_empty_stats_equal_share_broad_queries(session: Session) -> None:
    cfg = _settings()
    ensure_search_templates(session, cfg)
    plan = plan_cold_start(
        session,
        _pref(),
        total_slots=5,
        employer_names=("Acme Ltd",),
        settings=cfg,
    )
    assert plan.total_slots == 5
    ids = [item.template_id for item in plan.items]
    assert "CF1b" not in ids
    assert set(ids) == {"CF1", "CF2", "CF3", "BD1", "BD2"}
    assert sum(item.request_slots for item in plan.items) == 5
    assert all(item.request_slots == 1 for item in plan.items)
    by_id = {item.template_id: item for item in plan.items}
    assert "Software Engineer" in by_id["CF1"].query
    assert "London" in by_id["CF1"].query
    assert "hiring" in by_id["CF2"].query.lower() or "careers" in by_id["CF2"].query.lower()
    assert "Acme Ltd" in by_id["CF3"].query
    assert by_id["BD1"].site == "boards.greenhouse.io"
    assert by_id["BD2"].site == "jobs.lever.co"
    # Broad: role + city present; salary never appears.
    blob = " ".join(item.query for item in plan.items)
    assert "salary" not in blob.lower()
    assert "70000" not in blob


def test_t6_unproven_below_five_runs_not_zero(session: Session) -> None:
    cfg = _settings()
    ensure_search_templates(session, cfg)
    # CF2 has 3 runs (unproven); CF1 has 10 (proven). Unproven must still get slots.
    session.add(
        TemplateStats(
            template_id="CF1",
            run_count=10,
            total_cost=Decimal("0"),
            new_candidates=0,
            new_verified_boards=5,
            relevant_jobs=20,
            freshness_share=Decimal("1"),
            updated_at=datetime.now(UTC),
        )
    )
    session.add(
        TemplateStats(
            template_id="CF2",
            run_count=UNPROVEN_RUN_THRESHOLD - 2,
            total_cost=Decimal("1"),
            new_candidates=0,
            new_verified_boards=0,
            relevant_jobs=0,
            freshness_share=Decimal("0"),
            updated_at=datetime.now(UTC),
        )
    )
    session.flush()
    plan = plan_cold_start(
        session,
        _pref(),
        total_slots=10,
        employer_names=(),
        settings=cfg,
    )
    by_id = {item.template_id: item.request_slots for item in plan.items}
    assert by_id.get("CF2", 0) >= 1
    assert plan.exploration_slots >= 1
    assert sum(by_id.values()) == 10


@pytest.mark.asyncio
async def test_t9_cost_recorded_immediately(session: Session) -> None:
    cfg = _settings()
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    ensure_search_templates(session, cfg)
    _policy(session, Decimal("100"), now)
    applicant = _applicant(session)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.mojeek.com"
        qs = parse_qs(urlparse(str(request.url)).query)
        assert qs["api_key"] == ["test-mojeek-key"]
        body = {
            "response": {
                "status": "OK",
                "results": [
                    {
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                        "title": "Engineer",
                        "desc": "Build things",
                    }
                ],
            }
        }
        return httpx.Response(200, json=body)

    gateway = _gateway(handler)
    run = SearchRun(
        template_id="CF2",
        applicant_id=applicant.id,
        query="Software Engineer London hiring",
        provider="mojeek",
        status=SearchRunStatus.planned,
        next_attempt_at=now,
    )
    session.add(run)
    session.flush()

    outcome = await run_search(
        session,
        gateway,
        run,
        settings=cfg,
        clock=lambda: now,
    )
    await gateway.aclose()

    assert outcome.status == "succeeded"
    assert outcome.cost_amount == Decimal("0.003")
    session.refresh(run)
    assert run.status == SearchRunStatus.succeeded
    assert run.cost_amount == Decimal("0.003")
    assert run.result_count == 1
    assert run.finished_at == now
    results = session.scalars(
        select(SearchResult).where(SearchResult.search_run_id == run.id)
    ).all()
    assert len(results) == 1
    assert results[0].url.startswith("https://boards.greenhouse.io/")
    ledger = session.scalars(select(SpendLedger)).all()
    assert len(ledger) == 1
    assert ledger[0].actual_cost == Decimal("0.003")
    stats = session.get(TemplateStats, "CF2")
    assert stats is not None
    assert stats.run_count == 1
    assert stats.total_cost == Decimal("0.003")


@pytest.mark.asyncio
async def test_search_run_planned_to_running_to_succeeded_reed(session: Session) -> None:
    cfg = _settings()
    now = datetime(2026, 11, 5, 13, 0, tzinfo=UTC)
    ensure_search_templates(session, cfg)
    _policy(session, Decimal("0"), now)  # Reed is free of money caps
    applicant = _applicant(session)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.reed.co.uk"
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "jobUrl": "https://www.reed.co.uk/jobs/1",
                        "jobTitle": "Engineer",
                        "employerName": "Acme",
                        "jobDescription": "Python role",
                    }
                ]
            },
        )

    gateway = _gateway(handler)
    from app.search.planner import PlannedSearch, SearchPlan

    plan = SearchPlan(
        items=(
            PlannedSearch(
                template_id="CF1",
                provider="reed",
                query="Software Engineer London",
                site=None,
                request_slots=1,
            ),
        ),
        total_slots=1,
        exploration_slots=1,
    )
    runs = create_runs_from_plan(session, plan, applicant_id=applicant.id, now=now)
    assert runs[0].status == SearchRunStatus.planned

    outcome = await run_search(session, gateway, runs[0], settings=cfg, clock=lambda: now)
    await gateway.aclose()
    assert outcome.status == "succeeded"
    session.refresh(runs[0])
    assert runs[0].status == SearchRunStatus.succeeded
    assert runs[0].cost_amount == Decimal("0")
    assert runs[0].attempt_count == 1


@pytest.mark.asyncio
async def test_search_run_failed_schedules_retry(session: Session) -> None:
    cfg = _settings()
    now = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
    ensure_search_templates(session, cfg)
    _policy(session, Decimal("50"), now)
    applicant = _applicant(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "busy"})

    gateway = _gateway(handler)
    run = SearchRun(
        template_id="CF2",
        applicant_id=applicant.id,
        query="role city",
        provider="mojeek",
        status=SearchRunStatus.planned,
        next_attempt_at=now,
    )
    session.add(run)
    session.flush()

    outcome = await run_search(session, gateway, run, settings=cfg, clock=lambda: now)
    await gateway.aclose()
    assert outcome.status == "failed"
    session.refresh(run)
    assert run.status == SearchRunStatus.failed
    assert run.next_attempt_at == now + timedelta(minutes=1)
    assert run.attempt_count == 1
    assert run.cost_amount == Decimal("0")


@pytest.mark.asyncio
async def test_search_run_inconclusive_no_usable_urls(session: Session) -> None:
    cfg = _settings()
    now = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    ensure_search_templates(session, cfg)
    _policy(session, Decimal("50"), now)
    applicant = _applicant(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "response": {
                        "status": "OK",
                        "results": [{"url": "", "title": "Empty", "desc": "No link"}],
                    }
                }
            ).encode(),
            headers={"content-type": "application/json"},
        )

    gateway = _gateway(handler)
    run = SearchRun(
        template_id="CF2",
        applicant_id=applicant.id,
        query="role city",
        provider="mojeek",
        status=SearchRunStatus.planned,
        next_attempt_at=now,
    )
    session.add(run)
    session.flush()

    outcome = await run_search(session, gateway, run, settings=cfg, clock=lambda: now)
    await gateway.aclose()
    assert outcome.status == "inconclusive"
    session.refresh(run)
    assert run.status == SearchRunStatus.inconclusive
    assert run.next_attempt_at is None
    assert run.cost_amount == Decimal("0.003")


@pytest.mark.asyncio
async def test_budget_wait_leaves_run_planned(session: Session) -> None:
    cfg = _settings()
    now = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
    ensure_search_templates(session, cfg)
    _policy(session, Decimal("0"), now)
    applicant = _applicant(session)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"response": {"status": "OK", "results": []}})

    gateway = _gateway(handler)
    run = SearchRun(
        template_id="CF2",
        applicant_id=applicant.id,
        query="paid search",
        provider="mojeek",
        status=SearchRunStatus.planned,
        next_attempt_at=now,
    )
    session.add(run)
    session.flush()

    outcome = await run_search(session, gateway, run, settings=cfg, clock=lambda: now)
    await gateway.aclose()
    assert isinstance(outcome, SearchBudgetWait)
    session.refresh(run)
    assert run.status == SearchRunStatus.planned
    assert run.attempt_count == 0
    assert seen == []
