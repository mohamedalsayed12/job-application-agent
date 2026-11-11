"""Cleaning: the common format, duplicate links and closing (T22 to T26), for LinkedIn results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cleaning import close_stale, completeness, normalize_job, reclean_all
from app.cleaning.normalize import Location, parse_location
from app.models import (
    FetchOutcome,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobLinkMethod,
    JobPosting,
    RawPayload,
)
from tests.jobs import DAY_1, _job, _li, _store

ADVERT = "Build APIs in Python for our payments platform. " * 10


def test_a_linkedin_result_is_cleaned_into_the_common_format() -> None:
    raw = _li(
        7,
        title="Junior Python Developer",
        company="Monzo",
        city="Manchester, England, United Kingdom",
        content="&lt;p&gt;Build APIs in Python.&lt;/p&gt;",
        posted="2026-11-05T00:00:00.000Z",
    )
    job = normalize_job("linkedin", raw)
    assert (job.title, job.company, job.description) == ("Junior Python Developer", "Monzo", "Build APIs in Python.")
    assert (job.location.city, job.location.country) == ("Manchester", "GB")
    assert job.apply_url == "https://acme.example/careers/7"
    assert (job.hours, job.posted_at) == ("full_time", datetime(2026, 11, 5, tzinfo=UTC))
    assert not job.salary.known


def test_an_easy_apply_job_keeps_the_linkedin_link() -> None:
    job = normalize_job("linkedin", _li(7, applyUrl=None))
    assert job.apply_url == "https://www.linkedin.com/jobs/view/7"


@pytest.mark.parametrize(
    "raw",
    [{"title": "No company", "jobUrl": "https://www.linkedin.com/jobs/view/1"}, {"title": 5, "company": "Acme"}, {}],
)
def test_a_result_without_a_title_company_or_link_is_unusable(raw: dict) -> None:
    assert normalize_job("linkedin", raw) is None


def test_an_unknown_source_gives_no_job() -> None:
    assert normalize_job("greenhouse", _li(1)) is None


def test_t26_a_job_traces_back_to_its_raw_result_and_search(session: Session) -> None:
    _store(session, [_li(11)])
    job = _job(session, 11)
    posting = session.scalar(
        select(JobPosting).join(JobLink, JobLink.job_posting_id == JobPosting.id).where(JobLink.job_id == job.id)
    )
    raw = session.get(RawPayload, posting.raw_payload_id)
    run = session.get(FetchRun, raw.fetch_run_id)
    assert raw.payload["linkedinJobId"] == "11"
    assert (run.source, run.outcome) == ("linkedin", FetchOutcome.complete)
    assert (job.company_name, job.sector) == ("Acme", "tech")
    assert job.field_provenance["title"] == {
        "source": "linkedin",
        "posting_id": str(posting.id),
        "raw_payload_id": str(raw.id),
    }


def test_t26_missing_salary_is_unknown_and_never_zero(session: Session) -> None:
    _store(session, [_li(11)])
    job = _job(session, 11)
    assert (job.salary_known, job.salary_min, job.salary_max) == (False, None, None)
    assert "salary_min" not in job.field_provenance


def test_a_result_without_an_id_is_not_kept_and_unusable_ones_are_skipped(session: Session) -> None:
    _store(session, [{"title": "No id"}, {"linkedinJobId": "1"}, _li(2)])
    assert [row.source_job_id for row in session.scalars(select(JobPosting))] == ["2"]
    assert len(session.scalars(select(RawPayload)).all()) == 2


def test_a_job_found_again_updates_with_no_new_rows(session: Session) -> None:
    _store(session, [_li(11, title="Backend Developer")])
    _store(session, [_li(11, title="Backend Engineer")], at=DAY_1 + timedelta(days=1))
    assert _job(session, 11).title == "Backend Engineer"
    assert _job(session, 11).last_seen_at == DAY_1 + timedelta(days=1)
    assert len(session.scalars(select(JobPosting)).all()) == 1
    assert len(session.scalars(select(Job)).all()) == 1


def test_an_older_search_never_replaces_newer_data(session: Session) -> None:
    _store(session, [_li(11, title="New title")], at=DAY_1 + timedelta(days=1))
    _store(session, [_li(11, title="Old title")])
    assert _job(session, 11).title == "New title"


def test_t25_the_same_job_reposted_under_a_new_id_links_to_the_first(session: Session) -> None:
    _store(session, [_li(11, content=ADVERT)])
    _store(session, [_li(12, content=ADVERT, posted="2026-11-06T00:00:00.000Z")], at=DAY_1 + timedelta(days=1))
    assert _job(session, 12).id == _job(session, 11).id
    link = session.scalar(
        select(JobLink).join(JobPosting, JobPosting.id == JobLink.job_posting_id).where(JobPosting.source_job_id == "12")
    )
    assert (link.method, link.confidence) == (JobLinkMethod.strong_match, Decimal("0.9"))


@pytest.mark.parametrize(
    "difference",
    [
        {"title": "Senior Backend Developer"},
        {"city": "London, UK"},
        {"company": "Other Ltd"},
        {"content": "A different advert. " * 20},
        {"posted": "2026-09-01T00:00:00.000Z"},
    ],
    ids=["other_title", "other_city", "other_company", "other_advert", "months_apart"],
)
def test_t25_uncertain_pairs_stay_separate(session: Session, difference: dict) -> None:
    _store(session, [_li(11, content=ADVERT)])
    _store(session, [_li(12, **{"content": ADVERT, **difference})])
    assert _job(session, 12).id != _job(session, 11).id


def test_a_job_no_search_finds_for_a_while_closes_and_opens_again_when_found(session: Session) -> None:
    _store(session, [_li(11, title="Data Analyst"), _li(12)])
    _store(session, [_li(12)], at=DAY_1 + timedelta(days=10))
    closed = close_stale(session, source="linkedin", now=DAY_1 + timedelta(days=15), after=timedelta(days=14))
    session.expire_all()
    assert closed == 1
    assert (_job(session, 11).freshness, _job(session, 12).freshness) == (JobFreshness.closed, JobFreshness.open)
    assert _job(session, 11).closure_evidence == {"rule": "not_found_by_any_search_for_14_days"}
    assert close_stale(session, source="linkedin", now=DAY_1 + timedelta(days=15), after=timedelta(days=14)) == 0

    _store(session, [_li(11, title="Data Analyst")], at=DAY_1 + timedelta(days=16))
    assert (_job(session, 11).freshness, _job(session, 11).closure_evidence) == (JobFreshness.open, None)
    assert _job(session, 11).first_seen_at == DAY_1


def test_replaying_every_run_changes_nothing(session: Session) -> None:
    _store(session, [_li(11, title="Old title"), _li(12, title="Data Analyst")])
    _store(session, [_li(11, title="New title")], at=DAY_1 + timedelta(days=1))
    report = reclean_all(session)
    assert (report.postings_new, report.errors) == (0, [])
    assert _job(session, 11).title == "New title"
    assert len(session.scalars(select(JobLink)).all()) == 2
    assert len(session.scalars(select(Job)).all()) == 2


def test_completeness_is_reported_per_source_and_field(session: Session) -> None:
    _store(session, [_li(1), _li(2, title="Data Analyst")])
    result = completeness(session)["linkedin"]
    assert result["postings"] == 2
    assert result["title"] == 100.0
    assert result["location_city"] == 100.0
    assert result["salary"] == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bristol, UK", Location(city="Bristol", country="GB")),
        ("Dublin", Location(city="Dublin")),
        ("Dubai, United Arab Emirates", Location(city="Dubai", country="AE")),
        ("Lisbon, Portugal", Location(city="Lisbon", country="PT")),
        ("Gdansk, Pomeranian Voivodeship, Poland", Location(city="Gdansk", country="PL")),
        ("Chicago, Illinois", Location(city="Chicago", country="US")),
        ("Dublin, London", Location()),
        ("US Remote", Location(country="US", remote_type="remote")),
        ("Bristol, UK; Cambridge, UK", Location(country="GB")),
        ("London, UK; Dublin, Ireland", Location()),
        ("London, United Kingdom - Deliveroo", Location(city="London", country="GB")),
        ("London, England", Location(city="London", country="GB")),
        ("Austin, Texas, United States", Location(city="Austin", country="US")),
        ("New York, NY", Location(city="New York", country="US")),
        ("Office Based - London, UK", Location(city="London", country="GB")),
        ("Manchester (GB)", Location(city="Manchester", country="GB")),
        ("Portadown, Northern Ireland (GB)", Location(city="Portadown", country="GB")),
        ("United Kingdom", Location(country="GB")),
        ("Nationwide (GB)", Location(country="GB")),
        ("N/A", Location()),
        ("", Location()),
        ("Home based - EMEA", Location(remote_type="remote")),
        ("Home Based - Americas; Home based - EMEA", Location(remote_type="remote")),
        ("Cardiff, London or Remote (UK)", Location(remote_type="remote", country="GB")),
        ("Brighton, United Kingdom; London, United Kingdom - Deliveroo", Location(country="GB")),
        ("San Francisco, New York, Seattle, Chicago, Remote", Location(remote_type="remote")),
        ("San Francisco, New York, Seattle", Location()),
        ("Seattle or San Francisco", Location()),
    ],
)
def test_location_records_a_city_only_when_one_place_is_named(text: str, expected: Location) -> None:
    assert parse_location(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hybrid - Manchester", Location(city="Manchester", remote_type="hybrid")),
        ("Manchester (Hybrid)", Location(city="Manchester", remote_type="hybrid")),
        ("London, UK (Hybrid)", Location(city="London", country="GB", remote_type="hybrid")),
        ("Hybrid", Location(remote_type="hybrid")),
        ("Manchester - UK", Location(city="Manchester", country="GB")),
        ("UK - Manchester", Location(city="Manchester", country="GB")),
        ("Manchester - Piccadilly", Location()),
        ("Berlin, DE", Location(city="Berlin")),
        ("Sydney, AU", Location(city="Sydney", country="AU")),
        ("Toronto, ON", Location(city="Toronto", country="CA")),
        ("Seattle, WA", Location(city="Seattle", country="US")),
        ("Bengaluru, IN", Location(city="Bengaluru")),
        ("San Francisco, CA", Location(city="San Francisco")),
        ("Tbilisi, Georgia", Location()),
        ("Flexible", Location()),
        ("Anywhere", Location()),
        ("Multiple Locations", Location()),
    ],
)
def test_review_location_cases(text: str, expected: Location) -> None:
    assert parse_location(text) == expected


