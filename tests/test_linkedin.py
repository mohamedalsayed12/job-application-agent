"""D54: LinkedIn searches through the Apify scraper feed the job pipeline."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.linkedin import Search, collect_linkedin, searches
from app.models import (
    ApplicantProfile,
    BudgetPolicy,
    ExperienceLevel,
    FetchOutcome,
    FetchRun,
    JobFreshness,
    SpendLedger,
)
from tests.jobs import DAY_1, _Apify, _job, _li, _store
from tests.test_matching import _applicant

_ANY = frozenset({ExperienceLevel.entry, ExperienceLevel.mid, ExperienceLevel.senior})


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
            settings=Settings(**{"apify_api_token": "test-token", "anthropic_api_key": "", **settings}),  # type: ignore[arg-type]
            persist=session.flush,
            now=lambda: at,
            sleep=lambda seconds: None,
        )
    finally:
        await gateway.aclose()


def test_each_role_is_searched_in_each_location_once_however_many_people_want_it(session: Session) -> None:
    first = _applicant(session, roles=["Backend Developer", "Python Developer"], locations=["Manchester"])
    _applicant(session, roles=["backend developer"], locations=["manchester", "Leeds"])

    assert searches(session, None, per_person=10) == [
        Search("backend developer", "Leeds", _ANY),
        Search("Backend Developer", "Manchester", _ANY),
        Search("Python Developer", "Manchester", _ANY),
    ]
    assert searches(session, {first}, per_person=10) == [
        Search("Backend Developer", "Manchester", _ANY),
        Search("Python Developer", "Manchester", _ANY),
    ]


@pytest.mark.parametrize(
    ("levels", "codes"),
    [
        ([ExperienceLevel.entry], "2"),
        ([ExperienceLevel.internship, ExperienceLevel.entry], "1,2"),
        ([ExperienceLevel.entry, ExperienceLevel.mid], "2,3,4"),
        ([ExperienceLevel.mid, ExperienceLevel.senior], "3,4,5"),
        ([ExperienceLevel.senior], "4,5"),
    ],
)
def test_d63_a_search_asks_for_the_levels_the_person_chose(session: Session, levels: list[ExperienceLevel], codes: str) -> None:
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    session.add(ApplicantProfile(applicant_id=applicant, experience_levels=levels))
    session.flush()
    [search] = searches(session, None, per_person=10)
    assert parse_qs(urlsplit(search.url(posted_within_days=3)).query)["f_E"] == [codes]


def test_d63_people_share_a_search_only_when_linkedin_would_be_asked_the_same(session: Session) -> None:
    for levels in (["entry"], ["entry"], ["senior"], ["mid"], ["mid", "senior"]):
        applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
        session.add(ApplicantProfile(applicant_id=applicant, experience_levels=levels))
    session.flush()
    assert searches(session, None, per_person=10) == [
        Search("Backend Developer", "Manchester", frozenset({ExperienceLevel.entry})),
        Search("Backend Developer", "Manchester", frozenset({ExperienceLevel.mid})),
        Search("Backend Developer", "Manchester", frozenset({ExperienceLevel.mid, ExperienceLevel.senior})),
        Search("Backend Developer", "Manchester", frozenset({ExperienceLevel.senior})),
    ]


def test_a_search_asks_linkedin_for_recent_jobs_at_its_levels_newest_first() -> None:
    url = Search("Junior Python Developer", "Manchester", frozenset({ExperienceLevel.entry})).url(posted_within_days=3)
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
    assert "f_E=2%2C3%2C4%2C5" in body["startUrls"][0]["url"]
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


async def test_a_run_that_never_started_costs_nothing(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    report = await _collect(session, _Apify([_li(1)], start_status=500))

    assert report.failed == ["Backend Developer in Manchester: Apify answered 500"]
    assert session.scalars(select(SpendLedger)).one().actual_cost == Decimal(0)


async def test_a_run_that_does_not_finish_in_time_is_stopped(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_li(1)], status="RUNNING")

    report = await _collect(session, apify, linkedin_wait_minutes=1)

    assert report.failed == ["Backend Developer in Manchester: did not finish in time"]
    assert apify.requests[-1].url.path.endswith("/abort")


async def test_d8_jobs_do_not_close_when_every_search_failed(session: Session) -> None:
    _policy(session)
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _store(session, [_li(1)], at=DAY_1)
    later = DAY_1 + timedelta(days=15)

    report = await _collect(session, _Apify([], status="FAILED"), at=later)

    assert (report.searches, report.closed) == (1, 0)
    assert _job(session, 1).freshness == JobFreshness.open

    report = await _collect(session, _Apify([_li(2, title="Python Developer")]), at=later + timedelta(days=1))
    assert report.closed == 1
    session.expire_all()
    assert _job(session, 1).freshness == JobFreshness.closed


async def test_t4_jobs_do_not_close_when_the_budget_stopped_the_searches(session: Session) -> None:
    _policy(session, money_cap="0.01")
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _store(session, [_li(1)], at=DAY_1)

    report = await _collect(session, _Apify([_li(2)]), at=DAY_1 + timedelta(days=15))

    assert (report.stopped_by, report.searches, report.closed) == ("budget", 0, 0)
    assert _job(session, 1).freshness == JobFreshness.open


async def test_a_cycle_the_budget_cut_short_resumes_the_same_day_with_the_searches_it_missed(session: Session) -> None:
    settings = Settings(apify_api_token="test-token", anthropic_api_key="")
    one_search = settings.linkedin_max_results * settings.linkedin_unit_price + settings.linkedin_run_price
    _policy(session, money_cap=str(one_search * Decimal("1.5")))
    _applicant(session, roles=["Backend Developer"], locations=["Leeds", "Manchester"])

    first = _Apify([_li(1)], cost_usd=float(one_search))  # spends the cap's first search
    report = await _collect(session, first)
    assert (report.searches, report.stopped_by) == (1, "budget")
    assert "Leeds" in first.bodies()[0]["startUrls"][0]["url"]

    session.scalars(select(BudgetPolicy)).one().money_cap = Decimal("5")
    session.flush()
    resumed = _Apify([_li(2)])
    report = await _collect(session, resumed, at=DAY_1 + timedelta(hours=2))
    assert (report.searches, report.stopped_by) == (1, None)
    assert "Manchester" in resumed.bodies()[0]["startUrls"][0]["url"]

    done = _Apify([_li(3)])
    report = await _collect(session, done, at=DAY_1 + timedelta(hours=3))
    assert (report.searches, done.requests) == (0, [])
