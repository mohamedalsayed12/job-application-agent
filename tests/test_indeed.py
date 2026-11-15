"""Indeed searches through Apify's valig scraper feed the job pipeline (D54)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.indeed import collect_indeed, entry_titles, indeed_searches
from app.linkedin import Search
from app.models import BudgetPolicy, FetchOutcome, FetchRun, SpendLedger
from tests.jobs import DAY_1, _Apify, _indeed, _job
from tests.test_matching import _applicant

_ENTRY_BACKEND = [
    Search("Entry Level Backend Developer", "Manchester"),
    Search("Graduate Backend Developer", "Manchester"),
    Search("Junior Backend Developer", "Manchester"),
]


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


def test_each_role_becomes_graduate_junior_and_entry_level_titles() -> None:
    assert entry_titles("Backend Developer") == [
        "Graduate Backend Developer",
        "Junior Backend Developer",
        "Entry Level Backend Developer",
    ]
    assert entry_titles("Junior Python Developer") == [
        "Graduate Python Developer",
        "Junior Python Developer",
        "Entry Level Python Developer",
    ]
    assert entry_titles("  Graduate Software Engineer  ") == [
        "Graduate Software Engineer",
        "Junior Software Engineer",
        "Entry Level Software Engineer",
    ]


def test_indeed_searches_expand_each_role_once_per_location(session: Session) -> None:
    first = _applicant(session, roles=["Backend Developer", "Junior Python Developer"], locations=["Manchester"])
    _applicant(session, roles=["backend developer"], locations=["Manchester", "Leeds"])

    assert indeed_searches(session, None) == [
        Search("Entry Level Backend Developer", "Leeds"),
        Search("Entry Level Backend Developer", "Manchester"),
        Search("Entry Level Python Developer", "Manchester"),
        Search("Graduate Backend Developer", "Leeds"),
        Search("Graduate Backend Developer", "Manchester"),
        Search("Graduate Python Developer", "Manchester"),
        Search("Junior Backend Developer", "Leeds"),
        Search("Junior Backend Developer", "Manchester"),
        Search("Junior Python Developer", "Manchester"),
    ]
    assert indeed_searches(session, {first}) == [
        Search("Entry Level Backend Developer", "Manchester"),
        Search("Entry Level Python Developer", "Manchester"),
        Search("Graduate Backend Developer", "Manchester"),
        Search("Graduate Python Developer", "Manchester"),
        Search("Junior Backend Developer", "Manchester"),
        Search("Junior Python Developer", "Manchester"),
    ]


def test_backend_and_junior_backend_collapse_to_three_searches(session: Session) -> None:
    _applicant(session, roles=["Backend Developer", "Junior Backend Developer"], locations=["Manchester"])
    assert indeed_searches(session, None) == [
        Search("Entry Level Backend Developer", "Manchester"),
        Search("Graduate Backend Developer", "Manchester"),
        Search("Junior Backend Developer", "Manchester"),
    ]


async def test_a_search_starts_a_run_waits_for_it_and_stores_what_it_found(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1), _indeed(2, title="Python Developer")], cost_usd=0.01)

    report = await _collect(session, apify, applicant_id=applicant_id)

    # Three entry forms; the same two job ids only count as new on the first store.
    assert (report.searches, report.jobs, report.postings_new, report.failed) == (3, 6, 2, [])
    started = apify.requests[0]
    assert started.url.path == "/v2/acts/valig~indeed-jobs-scraper/runs"
    assert started.headers["Authorization"] == "Bearer test-token"
    assert "test-token" not in str(started.url)
    assert [body["title"] for body in apify.bodies()] == [search.role for search in _ENTRY_BACKEND]
    assert apify.bodies()[0] == {
        "country": "uk",
        "title": "Entry Level Backend Developer",
        "location": "Manchester",
        "limit": 50,
        "datePosted": "3",
    }
    assert _job(session, 2).title == "Python Developer"
    spent = list(session.scalars(select(SpendLedger)))
    assert len(spent) == 3
    assert {(row.kind, row.provider, row.actual_cost) for row in spent} == {("indeed_jobs", "apify", Decimal("0.0080"))}


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
    assert report.searches == 3


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

    assert report.failed == [f"{search.role} in Manchester: run failed" for search in _ENTRY_BACKEND]
    runs = list(session.scalars(select(FetchRun)))
    assert len(runs) == 3
    assert {(run.outcome, run.jobs_returned, run.source) for run in runs} == {(FetchOutcome.failed, 0, "indeed")}
    assert session.scalar(select(SpendLedger).limit(1)).actual_cost == Decimal("0.0080")


async def test_a_run_that_does_not_finish_in_time_is_stopped(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)], status="RUNNING")

    report = await _collect(session, apify, applicant_id=applicant_id, indeed_wait_minutes=1)

    assert report.failed == [f"{search.role} in Manchester: did not finish in time" for search in _ENTRY_BACKEND]
    assert apify.requests[-1].url.path.endswith("/abort")


async def test_job_id_falls_back_to_jk_query_when_key_is_missing(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    item = _indeed("abc123")
    del item["key"]
    report = await _collect(session, _Apify([item]), applicant_id=applicant_id)
    assert report.postings_new == 1
    assert _job(session, "abc123").title == "Backend Developer"
