"""Step 13: the four quality measures, the report and the admin page."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import BoardStatus, Job, JobFreshness, SpendKind, SpendLedger
from app.quality import build_report, store_results
from app.quality.checks import (
    Confidence,
    JobFacts,
    NotRecent,
    check_complete,
    check_job,
    check_real,
    check_recent,
    find_same_adverts,
)
from tests.test_budget_gate import _applicant_and_run
from tests.test_cleaning import DAY_1, _board, _Collector, _job

ADMIN = "test-admin-key-step13"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
ADVERT = "Build and run backend services in Python for our payments platform. " * 10
EMPLOYER = uuid.uuid4()


def _facts(**changes: object) -> JobFacts:
    base = JobFacts(
        job_id=uuid.uuid4(),
        employer_id=EMPLOYER,
        company_name="Acme",
        title="Backend Developer",
        description=ADVERT,
        apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
        places=[{"city": "Manchester", "country": "GB"}],
        remote_type=None,
        freshness=JobFreshness.open,
        last_seen_at=NOW - timedelta(hours=1),
        first_seen_at=NOW - timedelta(days=2),
        posted_at=NOW - timedelta(days=3),
        closes_at=None,
        board_status=BoardStatus.verified,
        official_domain="acme.com",
        source="greenhouse",
        sector="tech",
    )
    return replace(base, **changes)


def _gh(job_id: int, *, title: str = "Backend Developer", city: str | None = "Manchester, UK", content: str = ADVERT) -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": city},
        "first_published": "2026-11-01T10:00:00+00:00",
        "content": content,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"last_seen_at": NOW - timedelta(hours=47)}, None),
        ({"last_seen_at": NOW - timedelta(hours=49)}, NotRecent.not_seen_in_48_hours),
        ({"closes_at": NOW + timedelta(days=1)}, None),
        ({"closes_at": NOW - timedelta(minutes=1)}, NotRecent.past_closing_date),
        ({"freshness": JobFreshness.closed}, NotRecent.closed),
        ({"freshness": JobFreshness.unknown}, None),
    ],
)
def test_recent_means_listed_in_the_last_48_hours_and_before_closing(changes: dict, expected: NotRecent | None) -> None:
    assert check_recent(_facts(**changes), NOW) == expected


def test_a_listing_older_than_60_days_is_flagged_and_stays_recent() -> None:
    quality = check_job(_facts(posted_at=NOW - timedelta(days=61)), NOW)
    assert quality.older_than_60_days
    assert quality.recent
    assert not check_job(_facts(posted_at=NOW - timedelta(days=59)), NOW).older_than_60_days


@pytest.mark.parametrize(
    ("changes", "expected", "reason"),
    [
        ({}, Confidence.high, "verified official board"),
        ({"apply_url": "https://careers.acme.com/jobs/1"}, Confidence.high, "apply link on the company's domain"),
        ({"apply_url": "https://acme.com.evil.io/jobs/1"}, Confidence.medium, "not on the company's domain"),
        ({"board_status": BoardStatus.needs_reverify}, Confidence.medium, "needs checking again"),
        ({"board_status": None}, Confidence.low, "not from a verified official board"),
        ({"title": "Join our Talent Community"}, Confidence.medium, "not a specific role"),
        ({"title": "Can't find anything that suits you?"}, Confidence.medium, "not a specific role"),
        ({"description": "You must pay a registration fee before you start."}, Confidence.low, "asks the applicant for money"),
        ({"description": "Please message us on WhatsApp to apply."}, Confidence.low, "chat-only contact"),
    ],
)
def test_real_takes_the_lowest_signal_and_gives_its_reason(changes: dict, expected: Confidence, reason: str) -> None:
    confidence, reasons = check_real(_facts(**changes))
    assert confidence == expected
    assert any(reason in text for text in reasons)


def test_payment_company_wording_is_not_a_scam_sign() -> None:
    advert = "We process payments and handle transaction fees. You will pay down technical debt in our deposit systems."
    assert check_real(_facts(description=advert))[0] == Confidence.high


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({}, ()),
        ({"places": []}, ("location",)),
        ({"places": [], "remote_type": "remote"}, ()),
        ({"places": [{"city": None, "country": "GB"}]}, ()),
        ({"apply_url": None}, ("apply_url",)),
        ({"description": None}, ("full_description",)),
        ({"description": "A short snippet of the advert..."}, ("full_description",)),
    ],
)
def test_complete_asks_for_title_location_apply_link_and_full_description(changes: dict, missing: tuple) -> None:
    assert check_complete(_facts(**changes)) == missing


def test_salary_and_employer_are_not_needed_for_complete() -> None:
    assert check_job(_facts(employer_id=None, company_name=""), NOW).complete


LONDON = [{"city": "London", "country": "GB"}]


def test_the_same_advert_in_the_same_city_is_a_duplicate_and_in_another_city_is_not() -> None:
    first = _facts(posted_at=NOW - timedelta(days=30))
    same_city = _facts(posted_at=NOW - timedelta(days=20))
    other_city = _facts(posted_at=NOW - timedelta(days=10), places=LONDON)
    found = find_same_adverts([other_city, first, same_city])
    assert set(found) == {same_city.job_id, other_city.job_id}
    assert found[same_city.job_id].of_job == first.job_id
    assert not check_job(same_city, NOW, found[same_city.job_id]).unique
    assert check_job(other_city, NOW, found[other_city.job_id]).unique


def test_a_job_in_several_places_duplicates_a_job_in_any_one_of_them() -> None:
    several = _facts(posted_at=NOW - timedelta(days=5), places=[*LONDON, {"city": "Manchester", "country": None}])
    one = _facts()
    assert find_same_adverts([several, one])[one.job_id].shares_a_place


def test_a_later_job_is_matched_to_any_earlier_job_in_its_place_not_just_the_last() -> None:
    manchester = _facts(posted_at=NOW - timedelta(days=9))
    london = _facts(posted_at=NOW - timedelta(days=8), places=LONDON)
    manchester_again = _facts(posted_at=NOW - timedelta(days=7))
    found = find_same_adverts([manchester, london, manchester_again])
    assert found[manchester_again.job_id].of_job == manchester.job_id
    assert found[manchester_again.job_id].shares_a_place


@pytest.mark.parametrize(
    "other",
    [
        {"posted_at": NOW + timedelta(days=28)},
        {"employer_id": uuid.uuid4()},
        {"title": "Frontend Developer"},
        {"description": ADVERT + "A different team."},
    ],
)
def test_jobs_that_differ_are_not_duplicates(other: dict) -> None:
    assert find_same_adverts([_facts(), _facts(**other)]) == {}


def test_spacing_and_title_case_do_not_hide_a_duplicate() -> None:
    copy = _facts(title="BACKEND  developer", description=ADVERT.replace(" ", "\n"))
    assert copy.job_id in find_same_adverts([_facts(posted_at=NOW - timedelta(days=9)), copy])


def test_jobs_with_no_description_or_employer_are_not_called_duplicates() -> None:
    assert find_same_adverts([_facts(description=None), _facts(description=None)]) == {}
    assert find_same_adverts([_facts(employer_id=None), _facts(employer_id=None)]) == {}


@pytest.fixture
def collector(session: Session) -> _Collector:
    return _Collector(session)


async def test_the_report_measures_real_collected_jobs_against_the_targets(session: Session, collector: _Collector) -> None:
    _board(session)
    jobs = [_gh(i, title=f"Backend Developer {i}") for i in range(1, 19)]
    await collector.read(
        200,
        {
            "jobs": [
                *jobs,
                _gh(19, city=""),
                _gh(20, title="Same", city="Leeds, UK; York, UK"),
                _gh(21, title="Same", city="York, UK"),
                _gh(22, title="Same", city="Hull, UK"),
            ]
        },
    )

    report = build_report(session, now=DAY_1 + timedelta(hours=1))
    targets = {target.name: target for target in report.jobs.targets}
    assert report.jobs.total.jobs == 22
    assert targets["Complete"].value == "95.5%"
    assert targets["Complete"].met
    assert targets["Expired jobs shown"].met
    assert targets["Duplicates"].value == "4.5%"
    assert not targets["Duplicates"].met
    assert report.jobs.same_advert_elsewhere == 1
    assert report.jobs.missing_counts == {"location": 1}
    assert report.jobs.real_levels == {"high": 22}
    assert [row.label for row in report.jobs.by_source] == ["greenhouse"]
    assert report.jobs.by_source[0].last_success == report.jobs.total.last_success is not None
    assert [row.label for row in report.jobs.by_sector] == ["tech"]


async def test_open_jobs_not_seen_for_48_hours_count_as_expired_jobs_shown(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh(1)]})
    expired = {t.name: t for t in build_report(session, now=DAY_1 + timedelta(hours=49)).jobs.targets}["Expired jobs shown"]
    assert expired.value == "1"
    assert not expired.met


async def test_a_closed_job_is_not_shown_or_counted_as_a_duplicate(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh(1, city="Leeds, UK; York, UK"), _gh(2, city="York, UK")]})
    _job(session, 1).freshness = JobFreshness.closed
    session.flush()
    report = build_report(session, now=DAY_1 + timedelta(hours=1))
    assert report.jobs.total.live == 1
    assert report.jobs.total.duplicate_pct == 0.0
    assert report.jobs.total.expired_pct == 50.0
    assert report.jobs.not_recent_counts == {"closed": 1}


async def test_results_are_stored_on_each_job_and_can_be_run_again(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh(1, city="Leeds, UK; York, UK"), _gh(2, city="York, UK")]})
    assert store_results(session, now=DAY_1) == 2
    assert store_results(session, now=DAY_1 + timedelta(days=3)) == 2
    first, second = _job(session, 1), _job(session, 2)
    assert first.quality_results["real"] == "high"
    assert first.quality_results["complete"] is True
    assert first.quality_results["recent"] is False
    assert first.quality_results["not_recent"] == "not_seen_in_48_hours"
    assert {first.quality_results["duplicate_of"], second.quality_results["duplicate_of"]} in (
        {None, str(first.id)},
        {None, str(second.id)},
    )
    assert [first.quality_results["unique"], second.quality_results["unique"]].count(False) == 1


async def test_the_brake_shows_as_an_event_only_on_the_fetch_where_jobs_vanished(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh(i, title=f"Role {i}") for i in range(1, 7)]})
    await collector.read(200, {"jobs": [_gh(1, title="Role 1"), _gh(2, title="Role 2")]})
    await collector.read(200, {"jobs": [_gh(1, title="Role 1"), _gh(2, title="Role 2")]})
    events = build_report(session, now=DAY_1 + timedelta(days=3)).collection.brake_events
    assert [(name, gone, before) for name, _, gone, before in events] == [("Acme", 4, 6)]


async def test_failed_reads_show_in_outcomes_and_repeated_failures(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(500, b"", on_next_day=False)
    collector.at += timedelta(days=1)
    await collector.read(500, b"")
    collection = build_report(session, now=DAY_1 + timedelta(days=3)).collection
    assert sum(collection.outcomes_by_source["greenhouse"].values()) >= 1
    assert collection.repeated_failures and collection.repeated_failures[0][0] == "Acme"


def test_template_cost_comes_from_the_spend_ledger(session: Session) -> None:
    _, run_id = _applicant_and_run(session)
    for actual, estimated in ((Decimal("0.01"), Decimal("0.5")), (None, Decimal("0.02"))):
        session.add(
            SpendLedger(
                provider="anthropic",
                kind=SpendKind.ai_extraction,
                related_run_id=run_id,
                estimated_cost=estimated,
                actual_cost=actual,
                units=Decimal("1"),
                unit_label="call",
                occurred_at=NOW,
            )
        )
    session.flush()
    discovery = build_report(session, now=NOW).discovery
    assert [(runs, spent) for _, runs, spent in discovery.searches_by_template] == [(1, Decimal("0.03"))]
    assert discovery.spend_total == Decimal("0.03")
    assert discovery.spend_this_month == Decimal("0.03")


def test_an_empty_database_gives_a_report_that_meets_nothing(session: Session) -> None:
    report = build_report(session, now=NOW)
    assert report.jobs.total.jobs == 0
    assert not any(target.met for target in report.jobs.targets)
    assert report.audit.latest_day is None


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = lambda: session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
    get_settings.cache_clear()


async def test_the_admin_page_needs_the_key(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/quality")).status_code in (401, 403, 422)
    assert (await client.get("/admin/quality?admin_key=wrong")).status_code in (401, 403)


async def test_the_admin_page_shows_the_report_and_escapes_job_text(
    client: httpx.AsyncClient, session: Session, collector: _Collector
) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh(1, title="<script>alert(1)</script>", city=""), _gh(2, title="Other role")]})
    before = {job.id: job.quality_results for job in session.scalars(select(Job))}

    response = await client.get("/admin/quality", headers={"X-Admin-Key": ADMIN})

    assert response.status_code == 200
    for heading in ("Targets", "Jobs", "Discovery", "Collection", "Audit", "What this report cannot show yet"):
        assert f"<h2>{heading}</h2>" in response.text
    assert "<script>" not in response.text
    assert "Summaries: not built yet" in response.text
    assert before == {job.id: job.quality_results for job in session.scalars(select(Job))}
