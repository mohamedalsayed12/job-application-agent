"""D45: jobs whose advert requires a degree subject the applicant didn't study are hidden."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.applicants.cv_read import grounded
from app.matching.degrees import (
    STEM,
    Field,
    degree_shuts_out,
    fields_of,
    required_fields,
)
from app.matching.match import match_all
from app.models import ApplicantProfile, ExperienceLevel
from tests.jobs import _job, _store
from tests.test_matching import SOON, _applicant, _matches, _posting

BUSINESS = ["BSc Business Management"]
FILLER = "Build and run backend services in Python for our payments platform. " * 4


@pytest.mark.parametrize(
    ("advert", "fields"),
    [
        ("Undergraduate degree in Computer Science or STEM. Drive and a track record.", [STEM]),
        ("Bachelor’s degree in Electrical Engineering, Computer Engineering, Computer Science, or related discipline",
         [frozenset({Field.engineering, Field.computing})]),
        ("Degree in Software Engineering or Computer Science Experience with Linux and Python",
         [frozenset({Field.engineering, Field.computing})]),
        ("Bachelor's Degree in a STEM discipline", [STEM]),
        ("New grad or 0-2 years of experience, with a CS or engineering degree.",
         [frozenset({Field.computing, Field.engineering})]),
        ("Undergraduate degree in business, finance, economics, technology or other quantitative field",
         [frozenset({Field.business, Field.economics, Field.computing}) | STEM]),
        ("Ideas are a bonus, not a requirement. Minimum requirements Currently enrolled in a Bachelor’s program in "
         "Computer Science or related field", [frozenset({Field.computing})]),
        ("Degree in Computer Science and strong technical skills.", [frozenset({Field.computing})]),
        ("Degree in Computer Engineering or a related field Introductory experience with embedded C",
         [frozenset({Field.computing, Field.engineering})]),
    ],
)
def test_d45_a_required_subject_is_read_from_the_advert(advert: str, fields: list[frozenset[str]]) -> None:
    assert required_fields(advert) == fields


@pytest.mark.parametrize(
    "advert",
    [
        "Undergraduate degree in Computer Science or STEM, or a compelling narrative about your alternative path",
        "Bachelor’s degree in Computer Science, Cybersecurity, or equivalent practical experience",
        "Bachelor's level degree, preferably in engineering or computer science",
        "Preferred qualifications MS or PhD degree in ML/AI or a related field (e.g., math, physics)",
        "We do not ask for formal qualifications or degree requirements for any of our engineering roles.",
        "You hold at least a Level 3 qualification or a relevant engineering degree.",
        "This role demands a high degree of technical skill and autonomy.",
        "Experience using Google Drive, MS Office, CRM systems and other IT based technologies",
        "A degree with a good level of Maths from High School",
        "Bachelor's degree required.",
        "Strong technical skills and a passion for learning.",
        "A technical background with strong technical skills in Python.",
        "Degree in Computer Science or equivalent industry experience.",
        "Degree in Computer Science, or demonstrable experience building software.",
        "BSc in Computer Science or 3 years of relevant experience.",
        "Degree in Computer Science (or experience in lieu of a degree).",
        "Degree in Computer Science; experience can replace this.",
        "Computer Science degree, or a portfolio of work that shows your ability.",
        "A computer science or engineering degree, or similar technical experience.",
    ],
)
def test_d45_optional_or_subjectless_degrees_require_nothing(advert: str) -> None:
    assert required_fields(advert) == []


def test_d45_what_a_degree_counts_as() -> None:
    assert fields_of("BSc Business Management") == {Field.business}
    assert fields_of("BSc (Hons) Computer Science") == {Field.computing}
    assert fields_of("MEng Mechanical Engineering") == {Field.engineering}


@pytest.mark.parametrize(
    ("advert", "degrees", "hidden"),
    [
        ("Bachelor's degree in Computer Science or STEM.", BUSINESS, True),
        ("Bachelor's degree in Computer Science or STEM.", ["BSc Mathematics"], False),
        ("Bachelor's degree in Computer Science or STEM.", [*BUSINESS, "MSc Data Science"], False),
        ("Bachelor's degree in Computer Science or STEM.", [], False),
        ("Degree in Business, Finance or Economics.", BUSINESS, False),
        ("Degree in Computer Science or equivalent experience.", BUSINESS, False),
    ],
)
def test_d45_a_job_is_hidden_only_when_no_degree_covers_its_subject(advert: str, degrees: list[str], hidden: bool) -> None:
    assert degree_shuts_out(required_fields(advert), degrees) is hidden


async def test_d45_matching_leaves_out_jobs_needing_another_subject(session: Session) -> None:
    _store(
        session,
        [
        _posting(1, title="Junior Backend Developer", content=f"{FILLER} You have a degree in Computer Science or STEM."),
        _posting(2, title="Junior Backend Developer", content=f"{FILLER} Any degree, or none, is welcome."),
        ],
    )
    business = _applicant(session, roles=["Junior Backend Developer"], locations=["Manchester"])
    unstated = _applicant(session, roles=["Junior Backend Developer"], locations=["Manchester"])
    session.add(ApplicantProfile(applicant_id=business, experience_level=ExperienceLevel.entry, degrees=BUSINESS))
    session.add(ApplicantProfile(applicant_id=unstated, experience_level=ExperienceLevel.entry))
    session.flush()

    match_all(session, now=SOON)

    first, second = _job(session, 1).id, _job(session, 2).id
    assert set(_matches(session, business)) == {second}
    assert set(_matches(session, unstated)) == {first, second}


def test_d45_degrees_read_from_a_cv_must_be_in_it() -> None:
    cv = "Sam Example\nBSc (Hons) Business Management, University of Manchester, 2027"
    found = grounded({"degrees": ["BSc Business Management", "MSc Computer Science"]}, cv)
    assert found.degrees == ["BSc Business Management"]
