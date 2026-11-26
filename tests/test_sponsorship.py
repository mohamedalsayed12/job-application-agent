"""D79: visa sponsorship from application answers reaches matching and the judge profile."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.matching.match import match_all
from app.matching.sponsorship import (
    job_excluded_for_sponsorship,
    may_sponsor_signal,
    refuses_sponsorship,
)
from app.matching.verdicts import load_profiles
from app.models import ApplicationAnswers
from tests.jobs import _job, _store
from tests.test_matching import SOON, _applicant, _matches, _posting

ADVERT = "Build and run backend services in Python for our payments platform. " * 10


@pytest.mark.parametrize(
    "phrase",
    [
        "We are unable to sponsor visas for this role.",
        "The company cannot sponsor work visas.",
        "Sorry, we can't sponsor at this time.",
        "No sponsorship is available for this position.",
        "Applicants must have the right to work in the UK.",
        "You must already have the right to work in the UK.",
        "We do not offer sponsorship.",
        "We don't offer sponsorship for Skilled Worker visas.",
        "Candidates must already have existing UK work authorisation.",
        "Right to work in the UK is essential.",
    ],
)
def test_common_uk_no_sponsor_phrases_are_detected(phrase: str) -> None:
    assert refuses_sponsorship(f"{ADVERT} {phrase}")


@pytest.mark.parametrize(
    "phrase",
    [
        "We welcome applications from all backgrounds.",
        "You will need a UK driving licence.",
        "Sponsorship of team events is encouraged.",
        "Must have strong communication skills.",
    ],
)
def test_unrelated_wording_is_not_a_sponsorship_refusal(phrase: str) -> None:
    assert not refuses_sponsorship(f"{ADVERT} {phrase}")


def test_licensed_sponsor_is_only_a_weak_non_binding_signal() -> None:
    text = f"{ADVERT} We are a licensed sponsor on the Home Office register."
    assert may_sponsor_signal(text)
    assert not refuses_sponsorship(text)
    assert not job_excluded_for_sponsorship(needs_visa_sponsorship=True, description=text)


def test_exclusion_only_when_the_applicant_needs_sponsorship() -> None:
    no_sponsor = f"{ADVERT} We are unable to sponsor visas."
    assert job_excluded_for_sponsorship(needs_visa_sponsorship=True, description=no_sponsor)
    assert not job_excluded_for_sponsorship(needs_visa_sponsorship=False, description=no_sponsor)
    assert not job_excluded_for_sponsorship(needs_visa_sponsorship=None, description=no_sponsor)
    assert not job_excluded_for_sponsorship(needs_visa_sponsorship=True, description=ADVERT)


def _answers(session: Session, applicant_id: uuid.UUID, *, needs_sponsorship: bool, right_to_work: bool) -> None:
    session.add(
        ApplicationAnswers(
            applicant_id=applicant_id,
            right_to_work_uk=right_to_work,
            needs_visa_sponsorship=needs_sponsorship,
        )
    )
    session.flush()


async def test_a_sponsorship_needing_applicant_does_not_match_a_no_sponsor_job(session: Session) -> None:
    _store(
        session,
        [
            _posting(1, content=f"{ADVERT} We are unable to sponsor work visas for this role."),
            _posting(2, content=f"{ADVERT} Early-career Python role on a friendly team."),
        ],
    )
    needs = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    fine = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _answers(session, needs, needs_sponsorship=True, right_to_work=False)
    _answers(session, fine, needs_sponsorship=False, right_to_work=True)

    match_all(session, now=SOON)

    assert set(_matches(session, needs)) == {_job(session, 2).id}
    assert set(_matches(session, fine)) == {_job(session, 1).id, _job(session, 2).id}


async def test_load_profiles_includes_visa_lines_for_the_judge(session: Session) -> None:
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _answers(session, applicant, needs_sponsorship=True, right_to_work=False)

    profile = load_profiles(session, {applicant})[applicant]
    assert "Right to work in the UK: no" in profile.text
    assert "Needs visa sponsorship: yes" in profile.text
    assert profile.needs_visa_sponsorship is True
    assert profile.right_to_work_uk is False


async def test_load_profiles_omits_visa_lines_when_answers_are_absent(session: Session) -> None:
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    profile = load_profiles(session, {applicant})[applicant]
    assert "Needs visa sponsorship" not in profile.text
    assert profile.needs_visa_sponsorship is None
