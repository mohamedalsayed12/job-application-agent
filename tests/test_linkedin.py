"""D54: LinkedIn searches through the Apify scraper feed the job pipeline."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.linkedin import Search, collect_linkedin, searches
from app.models import BudgetPolicy, FetchOutcome, FetchRun, SpendLedger
from tests.jobs import DAY_1, _Apify, _job, _li
from tests.test_matching import _applicant


def _policy(session: Session, money_cap: str = "1") -> None:
    session.add(
        BudgetPolicy(
            provider="*", period="month", money_cap=Decimal(money_cap), currency="GBP", effective_from=DAY_1 - timedelta(days=30)
        )
    )
    session.flush()


async def _collect(session: Session, apify: _Apify, *, at=DAY_1, **settings: object):
    gateway = apify.gateway()
    try:
        return await collect_linkedin(
            session,
            gateway=gateway,
            settings=Settings(**{"apify_api_token": "test-token", **settings}),  # type: ignore[arg-type]
            persist=session.flush,
            now=lambda: at,
            sleep=lambda seconds: None,
        )
    finally:
        await gateway.aclose()


def test_each_role_is_searched_in_each_location_once_however_many_people_want_it(session: Session) -> None:
    first = _applicant(session, roles=["Backend Developer", "Python Developer"], locations=["Manchester"])
    _applicant(session, roles=["backend developer"], locations=["manchester", "Leeds"])

    assert searches(session, None) == [
        Search("backend developer", "Leeds"),
        Search("Backend Developer", "Manchester"),
        Search("Python Developer", "Manchester"),
    ]
    assert searches(session, {first}) == [Search("Backend Developer", "Manchester"), Search("Python Developer", "Manchester")]


def test_a_search_asks_linkedin_for_recent_entry_level_jobs_newest_first() -> None:
    url = Search("Junior Python Developer", "Manchester").url(posted_within_days=3)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.linkedin.com/jobs/search/?")
    assert query == {
        "keywords": ["Junior Python Developer"],
        "location": ["Manchester"],
        "f_E": ["2"],
        "f_TPR": ["r259200"],
        "sortBy": ["DD"],
    }


async def test_a_search_starts_a_run_waits_for_it_and_stores_what_it_found(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_li(1), _li(2, title="Python Developer")], cost_usd=0.02)

    report = await _collect(session, apify)

    assert (report.searches, report.jobs, report.postings_new, report.failed) == (1, 2, 2, [])
    started = apify.requests[0]
    assert started.url.path == "/v2/acts/blackfalcondata~linkedin-job-scraper/runs"
    assert started.headers["Authorization"] == "Bearer test-token"
    assert "test-token" not in str(started.url)
    body = apify.bodies()[0]
    assert body["maxResults"] == 50
    assert "f_E=2" in body["startUrls"][0]["url"]
    assert apify.checks == 2
    assert _job(session, 2).title == "Python Developer"
    spent = session.scalars(select(SpendLedger)).one()
    assert (spent.kind, spent.provider, spent.actual_cost) == ("linkedin_jobs", "apify", Decimal("0.0160"))


async def test_searches_run_once_a_day(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _collect(session, _Apify([_li(1)]))

    later = _Apify([_li(2)])
    report = await _collect(session, later, at=DAY_1 + timedelta(hours=23))
    assert (report.searches, later.requests) == (0, [])

    report = await _collect(session, _Apify([_li(2)]), at=DAY_1 + timedelta(hours=24))
    assert report.searches == 1


async def test_without_an_apify_token_nothing_is_searched(session: Session) -> None:
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_li(1)])
    report = await _collect(session, apify, apify_api_token="")
    assert (report.stopped_by, apify.requests) == ("not_configured", [])


async def test_with_no_budget_left_no_run_is_started(session: Session) -> None:
    _policy(session, money_cap="0.01")
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_li(1)])
    report = await _collect(session, apify)
    assert (report.stopped_by, apify.requests) == ("budget", [])


async def test_a_failed_run_is_recorded_and_its_cost_still_counted(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    report = await _collect(session, _Apify([_li(1)], status="FAILED", cost_usd=0.01))

    assert report.failed == ["Backend Developer in Manchester: run failed"]
    run = session.scalars(select(FetchRun)).one()
    assert (run.outcome, run.jobs_returned) == (FetchOutcome.failed, 0)
    assert session.scalars(select(SpendLedger)).one().actual_cost == Decimal("0.0080")


async def test_a_run_that_does_not_finish_in_time_is_stopped(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_li(1)], status="RUNNING")

    report = await _collect(session, apify, linkedin_wait_minutes=1)

    assert report.failed == ["Backend Developer in Manchester: did not finish in time"]
    assert apify.requests[-1].url.path.endswith("/abort")
