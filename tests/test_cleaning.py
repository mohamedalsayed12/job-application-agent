"""Cleaning: the common format, duplicate links and freshness (T22 to T26)."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cleaning import (
    board_job_key,
    clean_run,
    completeness,
    link_posting,
    normalize_job,
    reclean_all,
)
from app.cleaning import clean as clean_module
from app.cleaning.clean import link_method_counts
from app.cleaning.normalize import Location, parse_lever_salary, parse_location
from app.collection import CollectionReport, collect_due
from app.fetch.gateway import FetchGateway
from app.models import (
    BoardStatus,
    Employer,
    EmployerBoard,
    EvidenceLevel,
    FetchOutcome,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobLinkMethod,
    JobPosting,
    RawPayload,
)

DAY_1 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
LEVER_ID = "324f1be5-ab7c-4c13-8314-3466217ccc75"


def _board(session: Session, *, system: str = "greenhouse") -> EmployerBoard:
    employer = Employer(name="Acme", cleaned_name="acme", official_domain="acme.example", careers_system=system)
    session.add(employer)
    session.flush()
    board = EmployerBoard(
        employer_id=employer.id,
        system=system,
        instance="greenhouse" if system == "greenhouse" else "lever_global",
        board_id="acme",
        evidence_level=EvidenceLevel.strong,
        status="verified",
    )
    session.add(board)
    session.flush()
    return board


def _gh_job(job_id: int, *, title: str | None = None, city: str = "Manchester, UK", posted: str = "2026-11-01T10:00:00+00:00") -> dict:
    return {
        "id": job_id,
        "title": title or f"Backend Developer {job_id}",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": city},
        "first_published": posted,
        "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;",
    }


class _Collector:
    """Runs real collection against a scripted board, one day apart."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.at = DAY_1
        self.report = CollectionReport()

    async def read(self, status: int, body: object, *, on_next_day: bool = True) -> CollectionReport:
        content = body if isinstance(body, bytes) else json.dumps(body).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=content, headers={"content-type": "application/json"})

        gateway = FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co"}),
            resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
            transport=httpx.MockTransport(handler),
        )
        self.report = await collect_due(self.session, gateway, clock=lambda: self.at)
        await gateway.aclose()
        if on_next_day:
            self.at += timedelta(days=1)
        return self.report

    async def listing(self, ids: Iterable[int]) -> CollectionReport:
        jobs = [_gh_job(i) for i in ids]
        return await self.read(200, {"jobs": jobs})


def _job(session: Session, source_job_id: int | str) -> Job:
    return session.scalar(
        select(Job)
        .join(JobLink, JobLink.job_id == Job.id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobPosting.source_job_id == str(source_job_id))
    )


@pytest.fixture
def collector(session: Session) -> _Collector:
    return _Collector(session)


async def test_t23_a_job_closes_after_two_complete_fetches_without_it(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1, 2])
    await collector.listing([1])
    assert _job(session, 2).freshness == JobFreshness.open
    report = await collector.listing([1])
    job = _job(session, 2)
    assert job.freshness == JobFreshness.closed
    assert report.closed == 1
    assert _job(session, 1).freshness == JobFreshness.open

    last_two = session.scalars(select(FetchRun).order_by(FetchRun.finished_at.desc()).limit(2)).all()
    assert job.closure_evidence["rule"] == "missing_from_2_consecutive_complete_fetches"
    assert set(job.closure_evidence["fetch_runs"]) == {str(run.id) for run in last_two}


async def test_a_closed_job_that_returns_is_open_again_and_keeps_its_first_seen(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1, 2])
    await collector.listing([1])
    await collector.listing([1])
    report = await collector.listing([1, 2])
    job = _job(session, 2)
    assert report.reopened == 1
    assert job.freshness == JobFreshness.open
    assert job.closure_evidence is None
    assert job.first_seen_at == DAY_1
    assert len(session.scalars(select(JobPosting)).all()) == 2


async def test_a_job_that_returns_after_one_miss_never_closes(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1, 2])
    await collector.listing([1])
    await collector.listing([1, 2])
    await collector.listing([1])
    assert _job(session, 2).freshness == JobFreshness.open


@pytest.mark.parametrize(
    "bad_read",
    [
        (500, {"error": "server"}),
        (404, {"error": "gone"}),
        (200, b""),
        (200, b'{"jobs": [{"id": 1'),
        (200, {"jobs": [_gh_job(1)], "meta": {"total": 50}}),
    ],
    ids=["server_error", "not_found", "empty_body", "half_finished", "partial"],
)
async def test_t22_bad_reads_never_count_towards_closing(session: Session, collector: _Collector, bad_read: tuple) -> None:
    _board(session)
    await collector.listing([1, 2])
    await collector.listing([1])
    for _ in range(4):
        report = await collector.read(*bad_read)
        assert report.closed == 0
    assert _job(session, 2).freshness != JobFreshness.closed
    report = await collector.listing([1])
    assert report.closed == 1


async def test_a_partial_read_that_lists_a_job_stops_it_closing(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1, 2])
    await collector.read(200, {"jobs": [_gh_job(2)], "meta": {"total": 50}})
    await collector.listing([1])
    assert _job(session, 2).freshness == JobFreshness.open


async def test_three_failed_reads_in_a_row_make_jobs_unknown_until_a_complete_read(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1])
    await collector.read(500, {})
    await collector.read(500, {})
    assert _job(session, 1).freshness == JobFreshness.open
    report = await collector.read(500, {})
    assert _job(session, 1).freshness == JobFreshness.unknown
    assert report.unknown == 1
    await collector.listing([1])
    assert _job(session, 1).freshness == JobFreshness.open


async def test_board_not_found_flags_it_makes_jobs_unknown_and_it_is_still_read(session: Session, collector: _Collector) -> None:
    board = _board(session)
    await collector.listing([1])
    await collector.read(404, {"error": "gone"})
    assert board.status == BoardStatus.needs_reverify
    assert _job(session, 1).freshness == JobFreshness.unknown
    report = await collector.listing([1])
    assert report.outcomes == {FetchOutcome.complete: 1}
    assert _job(session, 1).freshness == JobFreshness.open


async def test_t24_mass_vanishing_holds_closures_until_the_next_complete_fetch(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 7))
    await collector.listing(range(1, 6))
    report = await collector.listing([1, 2])
    assert report.braked_boards == ["greenhouse/acme"]
    assert report.closed == 0
    assert report.held == 1
    assert _job(session, 6).freshness == JobFreshness.open

    report = await collector.listing([1, 2])
    assert report.braked_boards == ["greenhouse/acme"]
    assert report.closed == 0
    assert report.held == 4
    assert {_job(session, i).freshness for i in (3, 4, 5, 6)} == {JobFreshness.open}

    report = await collector.listing([1, 2])
    assert report.braked_boards == []
    assert report.closed == 4
    assert {_job(session, i).freshness for i in (3, 4, 5, 6)} == {JobFreshness.closed}
    assert {_job(session, i).freshness for i in (1, 2)} == {JobFreshness.open}
    assert len(_job(session, 3).closure_evidence["fetch_runs"]) == 3


async def test_t24_a_board_returning_empty_twice_closes_nothing_until_a_third_read(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 7))
    for _ in range(2):
        report = await collector.read(200, {"jobs": []})
        assert report.closed == 0
    assert {_job(session, i).freshness for i in range(1, 7)} == {JobFreshness.open}
    report = await collector.read(200, {"jobs": []})
    assert report.closed == 6


async def test_t24_jobs_returning_at_the_extra_fetch_never_close(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 7))
    await collector.listing([1])
    await collector.listing([1])
    report = await collector.listing(range(1, 7))
    assert report.closed == 0
    assert {_job(session, i).freshness for i in range(1, 7)} == {JobFreshness.open}


async def test_t24_jobs_that_return_after_the_brake_stay_open(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 7))
    await collector.listing([1])
    report = await collector.listing(range(1, 7))
    assert report.closed == 0
    assert {_job(session, i).freshness for i in range(1, 7)} == {JobFreshness.open}


async def test_t24_a_job_already_closed_stays_closed_when_the_brake_comes_on(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 8))
    await collector.listing(range(1, 7))
    await collector.listing(range(1, 7))
    assert _job(session, 7).freshness == JobFreshness.closed
    report = await collector.listing([1])
    assert report.braked_boards == ["greenhouse/acme"]
    assert _job(session, 7).freshness == JobFreshness.closed


async def test_exactly_half_vanishing_does_not_brake(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 7))
    report = await collector.listing([1, 2, 3])
    assert report.braked_boards == []


async def test_small_board_has_no_brake_and_a_valid_empty_board_closes_jobs(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing(range(1, 5))
    await collector.read(200, {"jobs": []})
    report = await collector.read(200, {"jobs": []})
    assert report.closed == 4


def _reed_posting(session: Session, board: EmployerBoard | None = None, **fields: object) -> JobPosting:
    run = FetchRun(source="reed", outcome="complete", jobs_returned=1, request_count=1, started_at=DAY_1, finished_at=DAY_1)
    session.add(run)
    session.flush()
    raw = RawPayload(fetch_run_id=run.id, source="reed", source_job_id="r1", payload={}, payload_hash="x", fetched_at=DAY_1)
    session.add(raw)
    session.flush()
    values = {
        "source": "reed",
        "source_job_id": "r1",
        "raw_payload_id": raw.id,
        "title": "Backend Developer",
        "company_name": "Acme",
        "employer_type": "direct",
        "salary_known": False,
        "apply_url": "https://www.reed.co.uk/jobs/r1",
        "first_seen_at": DAY_1,
        "last_seen_at": DAY_1,
        "employer_id": board.employer_id if board else None,
    }
    posting = JobPosting(**(values | fields))
    session.add(posting)
    session.flush()
    return posting


async def test_t25_apply_link_with_a_board_job_id_links_to_that_job(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([11])
    posting = _reed_posting(session, apply_url="https://boards.greenhouse.io/acme/jobs/11?src=reed")
    link = link_posting(session, posting)
    assert link.job_id == _job(session, 11).id
    assert link.method == JobLinkMethod.exact_job_id
    assert link.confidence == Decimal("1")
    assert "11" in link.reason


async def test_t25_strong_match_links_a_job_from_another_source(session: Session, collector: _Collector) -> None:
    board = _board(session)
    await collector.read(200, {"jobs": [_gh_job(11, title="Backend Developer")]})
    posting = _reed_posting(
        session, board, title="  backend   DEVELOPER ", location_city="manchester",
        posted_at=datetime(2026, 11, 8, tzinfo=UTC),
    )
    link = link_posting(session, posting)
    assert link.job_id == _job(session, 11).id
    assert link.method == JobLinkMethod.strong_match
    assert link.confidence == Decimal("0.9")


@pytest.mark.parametrize(
    "difference",
    [
        {"title": "Senior Backend Developer", "location_city": "Manchester", "posted_at": datetime(2026, 11, 8, tzinfo=UTC)},
        {"title": "Backend Developer", "location_city": "London", "posted_at": datetime(2026, 11, 8, tzinfo=UTC)},
        {"title": "Backend Developer", "location_city": "Manchester", "posted_at": datetime(2026, 9, 1, tzinfo=UTC)},
        {"title": "Backend Developer", "location_city": None, "posted_at": datetime(2026, 11, 8, tzinfo=UTC)},
        {"title": "Backend Developer", "location_city": "Manchester", "posted_at": None},
    ],
    ids=["other_title", "other_city", "months_apart", "no_city", "no_date"],
)
async def test_t25_uncertain_pairs_stay_separate(session: Session, collector: _Collector, difference: dict) -> None:
    board = _board(session)
    await collector.read(200, {"jobs": [_gh_job(11, title="Backend Developer")]})
    link = link_posting(session, _reed_posting(session, board, **difference))
    assert link.job_id != _job(session, 11).id


async def test_t25_no_employer_means_no_strong_match(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh_job(11, title="Backend Developer")]})
    posting = _reed_posting(
        session, title="Backend Developer", location_city="Manchester", posted_at=datetime(2026, 11, 8, tzinfo=UTC)
    )
    assert link_posting(session, posting).job_id != _job(session, 11).id


async def test_t25_a_match_with_two_possible_jobs_stays_separate(session: Session, collector: _Collector) -> None:
    board = _board(session)
    await collector.read(
        200,
        {"jobs": [
            _gh_job(11, title="Backend Developer", posted="2026-10-20T10:00:00+00:00"),
            _gh_job(12, title="Backend Developer", posted="2026-10-01T10:00:00+00:00"),
        ]},
    )
    assert _job(session, 11).id != _job(session, 12).id
    posting = _reed_posting(
        session, board, title="Backend Developer", location_city="Manchester",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    link = link_posting(session, posting)
    assert link.job_id not in (_job(session, 11).id, _job(session, 12).id)


async def test_two_listings_on_one_board_with_different_adverts_stay_separate(session: Session, collector: _Collector) -> None:
    _board(session)
    twin_a = _gh_job(11, title="Software Engineer")
    twin_b = _gh_job(12, title="Software Engineer") | {"content": "&lt;p&gt;Payments team.&lt;/p&gt;"}
    await collector.read(200, {"jobs": [twin_a, twin_b]})
    assert _job(session, 11).id != _job(session, 12).id


async def test_same_job_posted_twice_on_a_board_closes_only_when_both_have_gone(session: Session, collector: _Collector) -> None:
    _board(session)
    twin_a = _gh_job(11, title="Backend Developer")
    twin_b = _gh_job(12, title="Backend Developer", posted="2026-11-03T10:00:00+00:00")
    await collector.read(200, {"jobs": [twin_a, twin_b]})
    assert _job(session, 11).id == _job(session, 12).id
    await collector.read(200, {"jobs": [twin_b]})
    await collector.read(200, {"jobs": [twin_b]})
    assert _job(session, 11).freshness == JobFreshness.open
    await collector.read(200, {"jobs": [_gh_job(99)]})
    await collector.read(200, {"jobs": [_gh_job(99)]})
    assert _job(session, 11).freshness == JobFreshness.closed


async def test_reed_posting_keeps_a_job_open_until_its_expiry(session: Session, collector: _Collector) -> None:
    board = _board(session)
    await collector.listing([11])
    posting = _reed_posting(
        session, board, apply_url="https://boards.greenhouse.io/acme/jobs/11",
        closes_at=DAY_1 + timedelta(days=4),
    )
    link_posting(session, posting)
    await collector.read(200, {"jobs": []})
    await collector.read(200, {"jobs": []})
    assert _job(session, 11).freshness == JobFreshness.open
    collector.at = DAY_1 + timedelta(days=5)
    await collector.read(200, {"jobs": []})
    assert _job(session, 11).freshness == JobFreshness.closed


async def test_t26_a_job_traces_back_to_its_raw_response_and_fetch_run(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([11])
    job = _job(session, 11)
    posting = session.scalar(
        select(JobPosting).join(JobLink, JobLink.job_posting_id == JobPosting.id).where(JobLink.job_id == job.id)
    )
    raw = session.get(RawPayload, posting.raw_payload_id)
    run = session.get(FetchRun, raw.fetch_run_id)
    assert raw.payload["id"] == 11
    assert run.outcome == FetchOutcome.complete
    assert job.field_provenance["title"] == {
        "source": "greenhouse",
        "posting_id": str(posting.id),
        "raw_payload_id": str(raw.id),
    }


async def test_t26_missing_salary_is_unknown_and_never_zero(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([11])
    job = _job(session, 11)
    assert (job.salary_known, job.salary_min, job.salary_max) == (False, None, None)
    assert "salary_min" not in job.field_provenance


async def test_lever_job_is_cleaned_with_salary_and_location(session: Session, collector: _Collector) -> None:
    _board(session, system="lever")
    raw = {
        "id": LEVER_ID,
        "text": "Platform Engineer",
        "applyUrl": f"https://jobs.lever.co/acme/{LEVER_ID}/apply",
        "hostedUrl": f"https://jobs.lever.co/acme/{LEVER_ID}",
        "categories": {"location": "Manchester", "commitment": "Full-time"},
        "country": "GB",
        "workplaceType": "hybrid",
        "createdAt": 1790772328805,
        "salaryRange": {"min": 3000, "max": 4000, "currency": "GBP", "interval": "per-month-salary"},
        "description": "<p>Run the platform.</p>",
        "lists": [{"text": "You", "content": "<li>Python</li>"}],
    }
    await collector.read(200, [raw])
    job = _job(session, LEVER_ID)
    posting = session.scalar(select(JobPosting))
    assert (job.title, job.location_city, job.location_country, job.remote_type) == ("Platform Engineer", "Manchester", "GB", "hybrid")
    assert (job.salary_known, job.salary_min, job.salary_max, job.salary_period) == (True, 36000, 48000, "year")
    assert job.apply_url.endswith("/apply")
    assert "Run the platform." in job.description and "Python" in job.description
    assert (posting.hours, posting.posted_at.year) == ("full_time", 2026)


async def test_html_escaped_advert_text_is_cleaned(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([11])
    assert _job(session, 11).description == "Build APIs in Python."


async def test_job_without_a_title_or_link_is_skipped_and_counted_as_seen_by_the_run(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [{"id": 1}, _gh_job(2)]})
    assert session.scalar(select(JobPosting)).source_job_id == "2"
    assert len(session.scalars(select(RawPayload)).all()) == 2


async def test_changed_advert_updates_the_job_with_no_new_rows(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh_job(11, title="Backend Developer")]})
    await collector.read(200, {"jobs": [_gh_job(11, title="Backend Engineer")]})
    assert _job(session, 11).title == "Backend Engineer"
    assert len(session.scalars(select(JobPosting)).all()) == 1
    assert len(session.scalars(select(Job)).all()) == 1


async def test_replaying_every_run_changes_nothing(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.read(200, {"jobs": [_gh_job(11, title="Old title"), _gh_job(12)]})
    await collector.read(200, {"jobs": [_gh_job(11, title="New title")]})
    await collector.read(200, {"jobs": [_gh_job(11, title="New title")]})
    closed_before = _job(session, 12).closure_evidence
    report = reclean_all(session, now=collector.at)
    assert report.postings_new == 0
    assert _job(session, 11).title == "New title"
    assert _job(session, 12).freshness == JobFreshness.closed
    assert _job(session, 12).closure_evidence == closed_before
    assert len(session.scalars(select(JobLink)).all()) == 2
    assert len(session.scalars(select(Job)).all()) == 2


async def test_cleaning_failure_keeps_the_fetch_and_a_replay_catches_up(
    session: Session, collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(session)

    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("parser bug")

    monkeypatch.setattr("app.collection.collect.clean_run", broken)
    report = await collector.listing([11])
    assert report.outcomes == {FetchOutcome.complete: 1}
    assert report.clean_errors == ["greenhouse/acme: ValueError: parser bug"]
    assert session.scalar(select(RawPayload)) is not None
    assert session.scalar(select(JobPosting)) is None
    monkeypatch.undo()
    reclean_all(session, now=collector.at)
    assert _job(session, 11).freshness == JobFreshness.open


async def test_a_run_that_missed_cleaning_cannot_close_a_job_listed_in_it(
    session: Session, collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(session)
    await collector.listing([1, 2])
    monkeypatch.setattr("app.collection.collect.clean_run", lambda *a, **k: 1 / 0)
    await collector.listing([1, 2])
    monkeypatch.undo()
    await collector.listing([1])
    run = session.scalars(select(FetchRun).order_by(FetchRun.finished_at.desc())).first()
    clean_run(session, run, board, now=collector.at)
    assert _job(session, 2).freshness == JobFreshness.open


async def test_completeness_is_reported_per_source_and_field(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1, 2])
    result = completeness(session)["greenhouse"]
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


@pytest.mark.parametrize(
    ("source", "raw"),
    [
        ("greenhouse", {"id": 1, "title": "x", "absolute_url": "https://x.example", "location": "Manchester"}),
        ("lever", {"id": "a", "text": "x", "applyUrl": "https://x.example", "lists": ["oops"]}),
        ("lever", {"id": "a", "text": "x", "applyUrl": "https://x.example", "country": 826}),
    ],
    ids=["location_string", "lists_string", "country_number"],
)
def test_wrong_typed_job_is_unusable_not_an_error(source: str, raw: dict) -> None:
    assert normalize_job(source, raw) is None


async def test_one_wrong_typed_job_does_not_stop_the_rest_of_the_board(session: Session, collector: _Collector) -> None:
    _board(session)
    bad = _gh_job(1) | {"location": "Manchester"}
    report = await collector.read(200, {"jobs": [bad, _gh_job(2)]})
    assert report.clean_errors == []
    assert session.scalar(select(JobPosting)).source_job_id == "2"


async def test_replay_carries_on_past_a_run_that_cannot_be_cleaned(
    session: Session, collector: _Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(session)
    await collector.listing([1])
    await collector.listing([1])
    real = clean_module.clean_run
    calls = {"count": 0}

    def fail_first(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("bad run")
        return real(*args, **kwargs)

    monkeypatch.setattr(clean_module, "clean_run", fail_first)
    report = reclean_all(session, now=collector.at)
    assert calls["count"] == 2
    assert len(report.errors) == 1
    assert report.postings_updated == 1


async def test_jobs_stay_unknown_after_not_found_until_a_complete_read(session: Session, collector: _Collector) -> None:
    _board(session)
    await collector.listing([1])
    await collector.read(404, {"error": "gone"})
    assert _job(session, 1).freshness == JobFreshness.unknown
    await collector.read(500, {})
    assert _job(session, 1).freshness == JobFreshness.unknown
    await collector.listing([1])
    assert _job(session, 1).freshness == JobFreshness.open


async def test_open_job_moves_to_a_live_listing_when_its_shown_listing_closes(session: Session, collector: _Collector) -> None:
    _board(session)
    twin_a = _gh_job(11, title="Backend Developer")
    twin_b = _gh_job(12, title="Backend Developer")
    await collector.read(200, {"jobs": [twin_a, twin_b]})
    job = _job(session, 11)
    assert job.apply_url.endswith("/11")
    await collector.read(200, {"jobs": [twin_b]})
    assert job.apply_url.endswith("/11")
    await collector.read(200, {"jobs": [twin_b]})
    assert job.freshness == JobFreshness.open
    assert job.apply_url.endswith("/12")
    assert job.field_provenance["apply_url"]["posting_id"] == str(
        session.scalar(select(JobPosting.id).where(JobPosting.source_job_id == "12"))
    )


async def test_two_listings_with_no_advert_text_stay_separate(session: Session, collector: _Collector) -> None:
    _board(session)
    twin_a = _gh_job(11, title="Backend Developer") | {"content": ""}
    twin_b = _gh_job(12, title="Backend Developer") | {"content": ""}
    await collector.read(200, {"jobs": [twin_a, twin_b]})
    assert _job(session, 11).id != _job(session, 12).id


async def test_first_postings_are_not_counted_as_exact_matches(session: Session, collector: _Collector) -> None:
    board = _board(session)
    await collector.listing([11])
    link_posting(session, _reed_posting(session, board, apply_url="https://boards.greenhouse.io/acme/jobs/11"))
    assert link_method_counts(session) == {"first_posting": 1, JobLinkMethod.exact_job_id: 1}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", ("greenhouse", "123")),
        ("https://notgreenhouse.io/acme/jobs/123", None),
    ],
)
def test_board_job_key_review_cases(url: str, expected: tuple[str, str] | None) -> None:
    assert board_job_key(url) == expected


def test_workplace_type_and_country_come_from_lever_fields() -> None:
    assert parse_location("Manchester", country="gb", workplace="hybrid") == Location(
        city="Manchester", country="GB", remote_type="hybrid"
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "£40k",
        {"min": 0, "max": 0, "currency": "GBP", "interval": "per-year-salary"},
        {"min": 30000, "max": 40000, "currency": "GBP", "interval": "whenever"},
        {"min": "n/a", "currency": "GBP", "interval": "per-year-salary"},
    ],
)
def test_unusable_salary_is_unknown(raw: object) -> None:
    assert parse_lever_salary(raw).known is False


def test_hourly_pay_is_kept_as_stated_because_annualising_needs_a_guess() -> None:
    salary = parse_lever_salary({"min": 20, "max": 25, "currency": "GBP", "interval": "per-hour-wage"})
    assert (salary.minimum, salary.maximum, salary.period) == (20, 25, "hour")


def test_unknown_source_gives_no_job() -> None:
    assert normalize_job("reed", {"title": "x"}) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123?src=reed", ("greenhouse", "123")),
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "123")),
        ("https://stripe.com/jobs/search?gh_jid=8172487", ("greenhouse", "8172487")),
        (f"https://jobs.lever.co/acme/{LEVER_ID}/apply", ("lever", LEVER_ID)),
        (f"https://jobs.eu.lever.co/acme/{LEVER_ID}", ("lever", LEVER_ID)),
        ("https://jobs.lever.co/acme", None),
        ("https://www.reed.co.uk/jobs/123", None),
        ("https://example.com/?gh_jid=abc", None),
        (None, None),
    ],
)
def test_board_job_key(url: str | None, expected: tuple[str, str] | None) -> None:
    assert board_job_key(url) == expected
