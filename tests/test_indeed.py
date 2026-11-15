"""Indeed searches through Apify's valig scraper feed the job pipeline (D54)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.indeed import collect_indeed
from app.models import BudgetPolicy, FetchOutcome, FetchRun, SpendLedger
from tests.jobs import DAY_1, _Apify, _indeed, _job
from tests.test_matching import _applicant


def _policy(session: Session, money_cap: str = "1") -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=Decimal(money_cap),
            currency="GBP",
            effective_from=DAY_1 - timedelta(days=30),
        )
    )
    session.flush()


async def _collect(session: Session, apify: _Apify, *, at=DAY_1, applicant_id: object | None = None, **settings: object):
    gateway = apify.gateway()
    try:
        # Empty string beats a process-env AI_APPLICANT_IDS that would hide the test applicant.
        opts = {"apify_api_token": "test-token", "ai_applicant_ids": str(applicant_id or ""), **settings}
        return await collect_indeed(
            session,
            gateway=gateway,
            settings=Settings(**opts),  # type: ignore[arg-type]
            persist=session.flush,
            now=lambda: at,
            sleep=lambda seconds: None,
        )
    finally:
        await gateway.aclose()


async def test_a_search_starts_a_run_waits_for_it_and_stores_what_it_found(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1), _indeed(2, title="Python Developer")], cost_usd=0.01)

    report = await _collect(session, apify, applicant_id=applicant_id)

    assert (report.searches, report.jobs, report.postings_new, report.failed) == (1, 2, 2, [])
    started = apify.requests[0]
    assert started.url.path == "/v2/acts/valig~indeed-jobs-scraper/runs"
    assert started.headers["Authorization"] == "Bearer test-token"
    assert "test-token" not in str(started.url)
    body = apify.bodies()[0]
    assert body == {
        "country": "uk",
        "title": "Backend Developer",
        "location": "Manchester",
        "limit": 50,
        "datePosted": "3",
    }
    assert apify.checks == 2
    assert _job(session, 2).title == "Python Developer"
    spent = session.scalars(select(SpendLedger)).one()
    assert (spent.kind, spent.provider, spent.actual_cost) == ("indeed_jobs", "apify", Decimal("0.0080"))


async def test_searches_run_once_a_day(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _collect(session, _Apify([_indeed(1)]), applicant_id=applicant_id)

    later = _Apify([_indeed(2)])
    report = await _collect(session, later, at=DAY_1 + timedelta(hours=23), applicant_id=applicant_id)
    assert (report.searches, later.requests) == (0, [])

    report = await _collect(
        session, _Apify([_indeed(2)]), at=DAY_1 + timedelta(hours=24), applicant_id=applicant_id
    )
    assert report.searches == 1


async def test_without_an_apify_token_nothing_is_searched(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)])
    report = await _collect(session, apify, applicant_id=applicant_id, apify_api_token="")
    assert (report.stopped_by, apify.requests) == ("not_configured", [])


async def test_with_no_budget_left_no_run_is_started(session: Session) -> None:
    _policy(session, money_cap="0.001")
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)])
    report = await _collect(session, apify, applicant_id=applicant_id)
    assert (report.stopped_by, apify.requests) == ("budget", [])


async def test_a_failed_run_is_recorded_and_its_cost_still_counted(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    report = await _collect(session, _Apify([_indeed(1)], status="FAILED", cost_usd=0.01), applicant_id=applicant_id)

    assert report.failed == ["Backend Developer in Manchester: run failed"]
    run = session.scalars(select(FetchRun)).one()
    assert (run.outcome, run.jobs_returned, run.source) == (FetchOutcome.failed, 0, "indeed")
    assert session.scalars(select(SpendLedger)).one().actual_cost == Decimal("0.0080")


async def test_a_run_that_does_not_finish_in_time_is_stopped(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)], status="RUNNING")

    report = await _collect(session, apify, applicant_id=applicant_id, indeed_wait_minutes=1)

    assert report.failed == ["Backend Developer in Manchester: did not finish in time"]
    assert apify.requests[-1].url.path.endswith("/abort")


async def test_job_id_falls_back_to_jk_query_when_key_is_missing(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    item = _indeed("abc123")
    del item["key"]
    report = await _collect(session, _Apify([item]), applicant_id=applicant_id)
    assert report.postings_new == 1
    assert _job(session, "abc123").title == "Backend Developer"
