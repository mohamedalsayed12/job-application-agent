"""Step 14: role, location and salary matching. T1, T2 and T27."""

from __future__ import annotations

import ast
import ipaddress
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applicants.service import create_applicant, parse_preference_input
from app.fetch.gateway import FetchGateway
from app.matching.match import (
    RoleKind,
    location_fit,
    match_all,
    match_role,
    wanted_locations,
)
from app.models import (
    ApplicantJobMatch,
    ApplicantProfile,
    ExperienceLevel,
    FetchRun,
    JobFreshness,
    RawPayload,
    SalaryStatus,
    SpendLedger,
)
from app.worker import run_cycle
from tests.test_cleaning import DAY_1, _board, _Collector, _job

ADVERT = "Build and run backend services in Python for our payments platform. " * 10
ENTRY_ROLES = ["Graduate Software Engineer", "Junior Python Developer", "Junior Backend Developer"]
SOON = DAY_1 + timedelta(hours=1)


def _gh(job_id: int, *, title: str = "Backend Developer", city: str = "Manchester, UK", content: str = ADVERT) -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": city},
        "first_published": "2026-11-01T10:00:00+00:00",
        "content": f"{content} Advert {job_id}.",
    }


def _applicant(session: Session, *, roles: list[str], locations: list[str], **salary: object) -> uuid.UUID:
    preference = parse_preference_input(target_roles=roles, locations=locations, **salary)
    applicant = create_applicant(session, email=f"{uuid.uuid4()}@example.com", preference=preference)
    session.flush()
    return applicant.id


def _matches(session: Session, applicant_id: uuid.UUID) -> dict[uuid.UUID, ApplicantJobMatch]:
    return {
        row.job_id: row
        for row in session.scalars(select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant_id))
    }


@pytest.mark.parametrize(
    ("title", "roles", "kind"),
    [
        ("Backend Developer", ["Backend Developer"], RoleKind.exact),
        ("Back-End Engineer, Payments", ["Backend Developer"], RoleKind.exact),
        ("Software Engineer, Backend", ["Backend Developer"], RoleKind.exact),
        ("Python Programmer", ["Python Developer"], RoleKind.exact),
        ("Software Engineer, New Grad", ENTRY_ROLES, RoleKind.exact),
        ("Graduate Software Engineer", ENTRY_ROLES, RoleKind.exact),
        ("Software Engineer", ["Backend Developer"], RoleKind.similar),
        ("Full Stack Developer", ["Junior Backend Developer"], RoleKind.similar),
        ("Backend Engineer", ["Software Engineer"], RoleKind.similar),
        ("Senior Backend Engineer", ["Backend Developer"], RoleKind.exact),
    ],
)
def test_titles_that_fit_a_target_role(title: str, roles: list[str], kind: RoleKind) -> None:
    assert match_role(title, ADVERT, roles).kind == kind


@pytest.mark.parametrize(
    ("title", "roles"),
    [
        ("Account Executive", ["Backend Developer"]),
        ("Data Engineer", ["Backend Developer"]),
        ("Backend Developer", ["Data Engineer"]),
        ("Sales Engineer", ["Software Engineer"]),
        ("Software Support Engineer", ["Backend Developer"]),
        ("Product Manager, Backend", ["Backend Developer"]),
        ("Engineering Director - Business Data", ["Data Engineer"]),
        ("Engineering Manager - Data Platform", ["Data Engineer"]),
        ("Software Engineering Manager (Backend SaaS)", ["Backend Developer"]),
        ("Software Support Engineer", ["Software Engineer"]),
        ("Software Engineer - L3 Support", ["Software Engineer"]),
        ("Senior Backend Engineer", ENTRY_ROLES),
        ("Staff Software Engineer", ENTRY_ROLES),
        ("Engineering Manager", ENTRY_ROLES),
        ("Software Engineer II", ENTRY_ROLES),
        ("Software Engineer, Intern", ENTRY_ROLES),
        ("Software Engineer Internship", ["Software Engineer"]),
        ("Software Engineering Placement", ENTRY_ROLES),
        ("Software Engineering Apprentice", ENTRY_ROLES),
    ],
)
def test_titles_that_do_not_fit(title: str, roles: list[str]) -> None:
    assert match_role(title, ADVERT, roles) is None


def test_a_manager_title_fits_when_the_applicant_asks_for_one() -> None:
    assert match_role("Engineering Manager, Data", ADVERT, ["Engineering Manager"]).kind == RoleKind.exact


def test_an_internship_fits_only_when_the_applicant_asks_for_one() -> None:
    assert match_role("Software Engineer, Intern", ADVERT, ["Software Engineer Intern"]) is not None


def test_an_exact_title_beats_a_similar_one_across_target_roles() -> None:
    fit = match_role("Python Developer", ADVERT, ["Junior Backend Developer", "Junior Python Developer"])
    assert (fit.target, fit.kind) == ("Junior Python Developer", RoleKind.exact)


def test_an_entry_level_applicant_loses_jobs_asking_for_3_years() -> None:
    advert = "You will have 5+ years of experience building APIs."
    assert match_role("Backend Developer", advert, ENTRY_ROLES) is None
    assert match_role("Backend Developer", advert, ["Backend Developer"]) is not None
    assert match_role("Graduate Backend Developer", advert, ENTRY_ROLES) is not None


def test_a_plain_title_says_its_level_is_not_stated() -> None:
    assert match_role("Backend Developer", ADVERT, ENTRY_ROLES).level == "level not stated"
    assert match_role("Junior Backend Developer", ADVERT, ENTRY_ROLES).level == "entry level in the title"
    assert match_role("Backend Developer", ADVERT, ["Backend Developer"]).level is None


UK = {"city": None, "country": "GB"}
MANCHESTER = {"city": "Manchester", "country": "GB"}
LONDON = {"city": "London", "country": "GB"}


@pytest.mark.parametrize(
    ("wants", "places", "remote_type", "fits"),
    [
        (["Manchester"], [{"city": None, "country": None, "region": "emea"}], "remote", True),
        (["Manchester"], [{"city": None, "country": None, "region": "worldwide"}], "remote", True),
        (["Manchester"], [{"city": None, "country": None, "region": "americas"}], "remote", False),
        (["Manchester"], [{"city": None, "country": None, "region": "apac"}], "remote", False),
        (
            ["Manchester"],
            [{"city": None, "country": None, "region": "americas"}, {"city": None, "country": None, "region": "emea"}],
            "remote",
            True,
        ),
        (["Remote UK"], [{"city": None, "country": None, "region": "emea"}], "remote", True),
        (["Remote UK"], [{"city": None, "country": None, "region": "apac"}], "remote", False),
        (["Berlin"], [{"city": None, "country": None, "region": "worldwide"}], "remote", True),
        (["Berlin"], [{"city": None, "country": None, "region": "emea"}], "remote", False),
        (["Manchester"], [MANCHESTER], None, True),
        (["manchester"], [LONDON, MANCHESTER], "hybrid", True),
        (["Manchester"], [{"city": "Manchester", "country": None}], None, True),
        (["Manchester"], [LONDON], None, False),
        (["Manchester"], [{"city": "Manchester", "country": "US"}], None, False),
        (["Manchester"], [UK], None, True),
        (["Manchester"], [{"city": None, "country": "US"}], None, False),
        (["Manchester"], [UK], "remote", True),
        (["Manchester"], [{"city": None, "country": "US"}], "remote", False),
        (["Manchester"], [], "remote", True),
        (["Manchester"], [], None, False),
        (["Berlin"], [{"city": None, "country": "US"}], "remote", False),
        (["Remote UK"], [UK], "remote", True),
        (["Remote UK"], [LONDON], "remote", True),
        (["Remote UK"], [LONDON], "hybrid", False),
        (["Remote UK"], [{"city": None, "country": "US"}], "remote", False),
        (["Remote"], [{"city": None, "country": "US"}], "remote", True),
        (["United Kingdom"], [MANCHESTER], None, False),
        (["United Kingdom"], [UK], None, True),
        (["Leeds", "London"], [LONDON], None, True),
    ],
)
def test_location_fit(wants: list[str], places: list[dict], remote_type: str | None, fits: bool) -> None:
    assert location_fit(wanted_locations(wants), places, remote_type)[0] is fits


async def test_t27_one_collection_is_matched_to_every_applicant(session: Session) -> None:
    _board(session)
    first = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    second = _applicant(session, roles=["Backend Developer"], locations=["London"])
    await _Collector(session).read(200, {"jobs": [_gh(1), _gh(2, title="Account Executive")]})
    fetches = session.scalar(select(func.count()).select_from(FetchRun))
    raws = session.scalar(select(func.count()).select_from(RawPayload))

    report = match_all(session, now=SOON)
    match_all(session, now=SOON)

    assert report.applicants == 2
    assert session.scalar(select(func.count()).select_from(FetchRun)) == fetches
    assert session.scalar(select(func.count()).select_from(RawPayload)) == raws
    job = _job(session, 1)
    assert set(_matches(session, first)) == set(_matches(session, second)) == {job.id}
    assert _matches(session, first)[job.id].location_fit is True
    assert _matches(session, second)[job.id].location_fit is False
    assert _matches(session, first)[job.id].score > _matches(session, second)[job.id].score


async def test_only_someone_whose_profile_chose_internship_is_matched_to_internships(session: Session) -> None:
    _board(session)
    seeker = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    session.add(ApplicantProfile(applicant_id=seeker, experience_level=ExperienceLevel.internship))
    graduate = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    session.add(ApplicantProfile(applicant_id=graduate, experience_level=ExperienceLevel.entry))
    no_profile = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    jobs = [_gh(1, title="Backend Developer Intern"), _gh(2), _gh(3, title="Backend Developer Apprentice")]
    await _Collector(session).read(200, {"jobs": jobs})

    match_all(session, now=SOON)

    internship, ordinary = _job(session, 1).id, _job(session, 2).id
    assert set(_matches(session, seeker)) == {internship}
    assert _matches(session, seeker)[internship].reasons["role"]["level"] == "internship in the title"
    assert set(_matches(session, graduate)) == {ordinary}
    assert set(_matches(session, no_profile)) == {ordinary}


async def test_t2_with_no_salary_given_no_job_is_excluded_for_salary(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _Collector(session).read(200, {"jobs": [_gh(1)]})
    match_all(session, now=SOON)
    row = _matches(session, applicant)[_job(session, 1).id]
    assert row.salary_status == SalaryStatus.unknown
    assert "known salary" in row.reasons["missing"]


def _lever_job(job_id: str, amount: int) -> dict:
    return {
        "id": job_id,
        "text": "Backend Developer",
        "applyUrl": f"https://jobs.lever.co/acme/{job_id}",
        "categories": {"location": "Manchester (GB)"},
        "descriptionPlain": ADVERT,
        "description": f"{ADVERT} {job_id}",
        "createdAt": 1793000000000,
        "salaryRange": {"min": amount, "max": amount, "currency": "GBP", "interval": "per-year-salary"},
    }


async def test_a_job_below_the_expected_salary_is_left_out_and_one_that_meets_it_scores_higher(session: Session) -> None:
    _board(session, system="lever")
    wants_pay = _applicant(
        session,
        roles=["Backend Developer"],
        locations=["Manchester"],
        salary_amount=Decimal("50000"),
        salary_currency="GBP",
        salary_period="year",
    )
    no_pay = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _Collector(session).read(200, [_lever_job("low", 30000), _lever_job("high", 60000)])
    match_all(session, now=SOON)

    low, high = _job(session, "low").id, _job(session, "high").id
    assert set(_matches(session, wants_pay)) == {high}
    assert _matches(session, wants_pay)[high].salary_status == SalaryStatus.meets
    assert set(_matches(session, no_pay)) == {low, high}
    assert _matches(session, wants_pay)[high].score > _matches(session, no_pay)[high].score


def test_t1_salary_is_not_read_by_search_ai_budget_or_collection_code() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    forbidden = {"salary_amount", "salary_currency", "salary_period", "ExpectedSalary", "expected_salary"}
    paths = [
        path
        for package in ("budget", "fetch", "search", "extraction", "discovery", "collection")
        for path in (root / package).rglob("*.py")
    ]
    paths.append(root / "matching" / "entry_level.py")
    assert len(paths) > 10
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            assert name not in forbidden, f"{path} reads {name}"


async def test_t1_matching_spends_nothing_and_stores_only_the_salary_status(session: Session) -> None:
    _board(session)
    applicant = _applicant(
        session,
        roles=["Backend Developer"],
        locations=["Manchester"],
        salary_amount=Decimal("76543"),
        salary_currency="GBP",
        salary_period="year",
    )
    await _Collector(session).read(200, {"jobs": [_gh(1)]})
    before = session.scalar(select(func.count()).select_from(SpendLedger))
    match_all(session, now=SOON)
    assert session.scalar(select(func.count()).select_from(SpendLedger)) == before
    assert "76543" not in str(_matches(session, applicant)[_job(session, 1).id].reasons)


@pytest.mark.parametrize("freshness", [JobFreshness.closed, JobFreshness.unknown])
async def test_only_open_jobs_match_and_a_match_goes_when_its_job_stops_being_open(session: Session, freshness: str) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _Collector(session).read(200, {"jobs": [_gh(1)]})
    match_all(session, now=SOON)
    assert len(_matches(session, applicant)) == 1

    _job(session, 1).freshness = freshness
    session.flush()
    report = match_all(session, now=SOON)
    session.expire_all()
    assert report.removed == 1
    assert _matches(session, applicant) == {}


async def test_a_job_not_seen_for_48_hours_does_not_match(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _Collector(session).read(200, {"jobs": [_gh(1)]})
    match_all(session, now=DAY_1 + timedelta(hours=49))
    assert _matches(session, applicant) == {}


async def test_one_advert_in_two_cities_gives_two_matches_and_a_duplicate_gives_one(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester", "London"])
    same = "The same advert. " * 30
    await _Collector(session).read(
        200,
        {
            "jobs": [
                {**_gh(1, city="Manchester, UK"), "content": same},
                {**_gh(2, city="London, UK"), "content": same},
                {**_gh(3, city="London, UK; Leeds, UK"), "content": same},
            ]
        },
    )
    match_all(session, now=SOON)
    assert len(_matches(session, applicant)) == 2


async def test_a_job_listed_in_several_places_matches_each_of_them(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _Collector(session).read(200, {"jobs": [_gh(1, city="Birmingham, UK; Manchester, UK; Leeds, UK")]})
    match_all(session, now=SOON)
    row = _matches(session, applicant)[_job(session, 1).id]
    assert row.location_fit is True
    assert row.reasons["location"] == "in Manchester"


async def test_the_worker_matches_after_it_collects(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"boards-api.greenhouse.io"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": [_gh(1)]})),
    )
    await run_cycle(session, gateway)
    await gateway.aclose()
    assert set(_matches(session, applicant)) == {_job(session, 1).id}


async def test_each_match_says_what_fitted_and_what_is_missing(session: Session) -> None:
    _board(session)
    applicant = _applicant(session, roles=ENTRY_ROLES, locations=["Manchester"])
    await _Collector(session).read(200, {"jobs": [_gh(1, title="Software Engineer", city="London, UK")]})
    match_all(session, now=SOON)
    row = _matches(session, applicant)[_job(session, 1).id]
    assert row.reasons["role"] == {"target": "Graduate Software Engineer", "kind": "exact", "level": "level not stated"}
    assert set(row.reasons["missing"]) == {"location", "stated entry level", "known salary"}
