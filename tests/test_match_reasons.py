"""Step 20: skills and licences in the match score, with reasons. T34."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.matching.credentials import Held, held_groups, required_in_advert
from app.matching.match import match_all
from app.models import ApplicantCertification, ApplicantJobMatch, ApplicantSkill
from app.quality.report import build_report
from app.skills.taxonomy import (
    label_index,
    load_taxonomy,
    map_job_skills,
    skill_for_text,
)
from tests.jobs import _job, _store
from tests.test_matching import SOON, _applicant, _matches, _posting

DRIVING = "UK driving licence"


@pytest.mark.parametrize(
    ("advert", "required"),
    [
        ("Full UK driving licence with no more than 6 points.", [DRIVING]),
        ("Possession of a valid UK driving licence is a must.", [DRIVING]),
        ("Qualified accountant (ACA, ACCA, CIMA or equivalent).", ["accountancy qualification"]),
        ("Project Management Professional (PMP) certification.", ["project management certification"]),
        ("Project Management Certification, preferably PMP or equivalent.", []),
        ("You're working towards a recognised security qualification, such as CISSP.", []),
        ("You must be eligible for SC clearance.", []),
        ("You hold active SC clearance.", ["security clearance"]),
        ("We are an aca demy of cpa ble people.", []),
        # Real wording where nearby optional words belong to something else.
        ("Full UK driving licence and willing to travel to site.", [DRIVING]),
        ("Netsuite preferred) Qualified Accountant (ACA, ACCA, CIMA) Advanced Excel", ["accountancy qualification"]),
        ("Accounting software (Netsuite preferred) Qualified Accountant (ACA, ACCA, CIMA)", ["accountancy qualification"]),
        ("Full UK driving licence with no more than 6 points £36,500 starting salary £2,500 a year bonus", [DRIVING]),
        ("B.S. degree in Accounting (CPA strongly preferred)", []),
        ("Build backend services in Python.", []),
        (None, []),
    ],
)
def test_the_licences_an_advert_requires(advert: str | None, required: list[str]) -> None:
    assert required_in_advert(advert) == required


def test_one_optional_mention_does_not_hide_a_required_one_elsewhere() -> None:
    advert = "A driving licence would be a bonus. " + "Filler about the team. " * 10 + "Full UK driving licence required."
    assert required_in_advert(advert) == [DRIVING]


def test_what_an_applicant_holds_is_read_however_they_typed_it() -> None:
    today = date(2026, 11, 9)
    held = [
        Held("Full UK driving licence", None, None),
        Held("acca", "ACCA", None),
        Held("CISSP", "ISC2", date(2026, 1, 1)),
        Held("Knitting badge", None, None),
    ]
    assert held_groups(held, today=today) == {DRIVING, "accountancy qualification"}


async def _job_and_applicant(
    session: Session, *, skills: list[str], certifications: list[str], content: str
) -> tuple[uuid.UUID, uuid.UUID]:
    load_taxonomy(session)
    index = label_index(session)
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    session.add_all(
        ApplicantSkill(applicant_id=applicant, text_as_typed=typed, skill_id=skill_for_text(index, typed))
        for typed in skills
    )
    session.add_all(ApplicantCertification(applicant_id=applicant, name=name) for name in certifications)
    _store(session, [_posting(1, content=content)])
    map_job_skills(session)
    match_all(session, now=SOON)
    return applicant, _job(session, 1).id


ADVERT = "We build backend services with Python, PostgreSQL and Docker. Full UK driving licence required. " * 5


async def test_t34_a_match_shows_skills_role_location_salary_and_licences(session: Session) -> None:
    applicant, job = await _job_and_applicant(session, skills=["python", "Postgres"], certifications=[], content=ADVERT)
    row = _matches(session, applicant)[job]
    assert row.matched_skills == ["PostgreSQL", "Python"]
    assert row.missing_skills == ["Docker"]
    assert row.missing_licences == [DRIVING]
    assert row.location_fit is True
    assert row.salary_status == "unknown"
    reasons = row.reasons
    assert reasons["role"]["kind"] == "exact"
    assert reasons["location"] == "in Manchester"
    assert reasons["skills"] == {"matched": ["PostgreSQL", "Python"], "missing": ["Docker"]}
    assert reasons["missing_licences"] == [DRIVING]
    assert {"skill: Docker", f"licence: {DRIVING}", "known salary"} <= set(reasons["missing"])


async def test_holding_the_licence_and_more_skills_raises_the_score(session: Session) -> None:
    weaker, job = await _job_and_applicant(session, skills=["Python"], certifications=[], content=ADVERT)
    stronger = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    index = label_index(session)
    session.add_all(
        ApplicantSkill(applicant_id=stronger, text_as_typed=typed, skill_id=skill_for_text(index, typed))
        for typed in ("Python", "Postgres", "Docker")
    )
    session.add(ApplicantCertification(applicant_id=stronger, name="Driving licence"))
    match_all(session, now=SOON)

    weak, strong = _matches(session, weaker)[job], _matches(session, stronger)[job]
    assert strong.missing_skills == [] and strong.missing_licences == []
    assert strong.score > weak.score
    assert weak.score == Decimal("0.700")  # exact role 0.5, location 0.3, a third of 0.3 for skills, licence -0.2
    assert strong.score == Decimal("1.100")


async def test_an_applicant_without_skills_is_told_to_add_them(session: Session) -> None:
    applicant, job = await _job_and_applicant(session, skills=[], certifications=[], content=ADVERT)
    row = _matches(session, applicant)[job]
    assert row.matched_skills == []
    assert "skills on profile" in row.reasons["missing"]


async def test_t34_relevance_is_reported_apart_from_the_quality_measures(session: Session) -> None:
    applicant, _ = await _job_and_applicant(session, skills=["Python"], certifications=["Driving licence"], content=ADVERT)
    before = build_report(session, now=SOON)
    assert before.relevance.matches == 1
    assert before.relevance.applicants_with_matches == 1
    assert before.relevance.skill_share_pct == pytest.approx(33.3)
    assert before.relevance.missing_licences == 0
    assert {target.name for target in before.jobs.targets} == {"Complete", "Expired jobs shown", "Duplicates"}

    session.execute(delete(ApplicantJobMatch))
    after = build_report(session, now=SOON)
    assert after.relevance.matches == 0
    assert after.jobs.total == before.jobs.total
