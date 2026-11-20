"""Indeed searches through Apify's valig scraper feed the job pipeline (D54)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.indeed import collect_indeed, indeed_searches, level_searches
from app.linkedin import Search
from app.models import (
    ApplicantProfile,
    BudgetPolicy,
    ExperienceLevel,
    FetchOutcome,
    FetchRun,
    JobFreshness,
    SpendLedger,
)
from tests.jobs import DAY_1, _Apify, _indeed, _job
from tests.test_matching import _applicant

_ENTRY = frozenset({ExperienceLevel.entry})
_ENTRY_BACKEND = [
    Search("Graduate Backend Developer", "Manchester", _ENTRY),
    Search("Junior Backend Developer", "Manchester", _ENTRY),
    Search("Entry Level Backend Developer", "Manchester", _ENTRY),
]


def _entry_applicant(session: Session, **preference: object) -> uuid.UUID:
    applicant_id = _applicant(session, **preference)  # type: ignore[arg-type]
    session.add(ApplicantProfile(applicant_id=applicant_id, experience_levels=["entry"]))
    session.flush()
    return applicant_id


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


def _titles(role: str, *levels: ExperienceLevel) -> list[str]:
    return [search.role for search in level_searches(Search(role, "Manchester", frozenset(levels)))]


def test_each_role_becomes_graduate_junior_and_entry_level_titles() -> None:
    entry = ["Graduate Backend Developer", "Junior Backend Developer", "Entry Level Backend Developer"]
    assert _titles("Backend Developer", ExperienceLevel.entry) == entry
    assert _titles("Junior Backend Developer", ExperienceLevel.entry) == entry
    assert _titles("  Graduate Backend Developer  ", ExperienceLevel.entry) == entry
    assert _titles("Graduate Junior Backend Developer", ExperienceLevel.entry) == entry


def test_d63_each_level_searches_its_own_title_forms() -> None:
    assert _titles("Backend Developer", ExperienceLevel.mid) == ["Backend Developer"]
    assert _titles("Senior Backend Developer", ExperienceLevel.senior) == ["Senior Backend Developer"]
    assert _titles("Backend Developer", ExperienceLevel.internship) == ["Backend Developer Intern"]
    assert _titles("Junior Backend Developer", ExperienceLevel.mid, ExperienceLevel.entry) == [
        "Graduate Backend Developer",
        "Junior Backend Developer",
        "Entry Level Backend Developer",
        "Backend Developer",
    ]


def test_indeed_searches_expand_each_role_once_per_location(session: Session) -> None:
    first = _entry_applicant(session, roles=["Backend Developer", "Junior Python Developer"], locations=["Manchester"])
    _entry_applicant(session, roles=["backend developer"], locations=["Manchester", "Leeds"])

    assert [(search.role, search.location) for search in indeed_searches(session, None)] == [
        ("Graduate Backend Developer", "Leeds"),
        ("Graduate Backend Developer", "Manchester"),
        ("Graduate Python Developer", "Manchester"),
        ("Junior Backend Developer", "Leeds"),
        ("Junior Backend Developer", "Manchester"),
        ("Junior Python Developer", "Manchester"),
        ("Entry Level Backend Developer", "Leeds"),
        ("Entry Level Backend Developer", "Manchester"),
        ("Entry Level Python Developer", "Manchester"),
    ]
    assert [(search.role, search.location) for search in indeed_searches(session, {first})] == [
        ("Graduate Backend Developer", "Manchester"),
        ("Graduate Python Developer", "Manchester"),
        ("Junior Backend Developer", "Manchester"),
        ("Junior Python Developer", "Manchester"),
        ("Entry Level Backend Developer", "Manchester"),
        ("Entry Level Python Developer", "Manchester"),
    ]


def test_backend_and_junior_backend_collapse_to_three_searches(session: Session) -> None:
    _entry_applicant(session, roles=["Backend Developer", "Junior Backend Developer"], locations=["Manchester"])
    assert indeed_searches(session, None) == _ENTRY_BACKEND


def test_d63_people_at_different_levels_share_the_titles_they_have_in_common(session: Session) -> None:
    _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    stepping_up = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    session.add(ApplicantProfile(applicant_id=stepping_up, experience_levels=["entry", "mid"]))
    session.flush()
    assert [search.role for search in indeed_searches(session, None)] == [
        "Graduate Backend Developer",
        "Junior Backend Developer",
        "Entry Level Backend Developer",
        "Backend Developer",
    ]


async def test_a_search_starts_a_run_waits_for_it_and_stores_what_it_found(session: Session) -> None:
    _policy(session)
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
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
        "title": "Graduate Backend Developer",
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
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _collect(session, _Apify([_indeed(1)]), applicant_id=applicant_id)

    later = _Apify([_indeed(2)])
    report = await _collect(session, later, at=DAY_1 + timedelta(hours=23), applicant_id=applicant_id)
    assert (report.searches, later.requests) == (0, [])

    report = await _collect(
        session, _Apify([_indeed(2)]), at=DAY_1 + timedelta(hours=24), applicant_id=applicant_id
    )
    assert report.searches == 3


async def test_without_an_apify_token_nothing_is_searched(session: Session) -> None:
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)])
    report = await _collect(session, apify, applicant_id=applicant_id, apify_api_token="")
    assert (report.stopped_by, apify.requests) == ("not_configured", [])


async def test_with_no_budget_left_no_run_is_started(session: Session) -> None:
    _policy(session, money_cap="0.001")
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)])
    report = await _collect(session, apify, applicant_id=applicant_id)
    assert (report.stopped_by, apify.requests) == ("budget", [])


async def test_a_failed_run_is_recorded_and_its_cost_still_counted(session: Session) -> None:
    _policy(session)
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    report = await _collect(session, _Apify([_indeed(1)], status="FAILED", cost_usd=0.01), applicant_id=applicant_id)

    assert report.failed == [f"{search.role} in Manchester: run failed" for search in _ENTRY_BACKEND]
    runs = list(session.scalars(select(FetchRun)))
    assert len(runs) == 3
    assert {(run.outcome, run.jobs_returned, run.source) for run in runs} == {(FetchOutcome.failed, 0, "indeed")}
    assert session.scalar(select(SpendLedger).limit(1)).actual_cost == Decimal("0.0080")


async def test_a_run_that_does_not_finish_in_time_is_stopped(session: Session) -> None:
    _policy(session)
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    apify = _Apify([_indeed(1)], status="RUNNING")

    report = await _collect(session, apify, applicant_id=applicant_id, indeed_wait_minutes=1)

    assert report.failed == [f"{search.role} in Manchester: did not finish in time" for search in _ENTRY_BACKEND]
    assert apify.requests[-1].url.path.endswith("/abort")


async def test_job_id_falls_back_to_jk_query_when_key_is_missing(session: Session) -> None:
    _policy(session)
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    item = _indeed("abc123")
    del item["key"]
    report = await _collect(session, _Apify([item]), applicant_id=applicant_id)
    assert report.postings_new == 1
    assert _job(session, "abc123").title == "Backend Developer"


async def test_d8_jobs_do_not_close_when_every_search_failed(session: Session) -> None:
    _policy(session)
    applicant_id = _entry_applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _collect(session, _Apify([_indeed(1)]), applicant_id=applicant_id)

    report = await _collect(
        session, _Apify([], status="FAILED"), at=DAY_1 + timedelta(days=15), applicant_id=applicant_id
    )

    assert (report.searches, report.closed) == (3, 0)
    assert _job(session, 1).freshness == JobFreshness.open
