"""D46: an AI verdict never puts an internship, an apprenticeship or a job asking for years of experience
in front of an entry-level person, nor pays for its fit report. (The degree cases are gone: D62 removed the degree filter.)"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.listings import load_listings
from app.applicants.cv import store_cv
from app.matching.judge import facts_text
from app.matching.match import match_all, showable
from app.matching.report import write_reports
from app.matching.verdicts import (
    PROMPT_VERSION,
    REPORT_PROMPT_VERSION,
    digest,
    load_profiles,
)
from app.models import (
    ApplicantProfile,
    ExperienceLevel,
    JobReport,
    JobVerdict,
    TailoredCv,
)
from app.tailor import write_tailored_cvs
from tests.jobs import _job, _store
from tests.test_cv_read import _docx
from tests.test_cv_tailor import _TailorAi
from tests.test_fit_report import CV_LINES, _Ai, _report, _run
from tests.test_judge import _policy, _settings
from tests.test_matching import ENTRY_ROLES, SOON, _applicant, _posting

FILLER = "Build backend services in Python and PostgreSQL for our payments platform. " * 4
HIDDEN = {
    1: ("Software Engineer Intern", "Join us for a 12-week summer internship."),
    2: ("Python Developer Degree Apprenticeship", "Earn while you learn on our apprenticeship."),
    3: ("Python Backend Developer", "You will need 3+ years of commercial Python experience."),
    4: ("Backend Developer", "An early-career role. You will need 2 years+ commercial experience with Python."),
}
SHOWN = {
    6: ("Junior Python Developer", "You will join a friendly team."),
    7: ("Backend Developer", "You have 2+ years of experience with Python. Recent graduates welcome."),
    8: ("Python Developer", "You will pair with experienced engineers."),
    5: ("Graduate Python Developer", "You have a degree in Computer Science or STEM."),
}


async def _entry_level_person(session: Session) -> uuid.UUID:
    _policy(session, Decimal(1))
    _store(
        session,
        [_posting(n, title=title, content=f"{FILLER} {extra}") for n, (title, extra) in (HIDDEN | SHOWN).items()],
    )
    applicant = _applicant(session, roles=ENTRY_ROLES, locations=["Manchester"])
    session.add(
        ApplicantProfile(applicant_id=applicant, experience_levels=[ExperienceLevel.entry])
    )
    store_cv(session, applicant, _docx(CV_LINES))
    session.flush()
    return applicant


def _ai_says_every_job_fits(session: Session, applicant: uuid.UUID) -> None:
    profile = load_profiles(session, {applicant})[applicant]
    for facts in showable(session, SOON):
        session.add(
            JobVerdict(
                applicant_id=applicant,
                job_id=facts.job_id,
                fits=True,
                level="entry",
                reason="It is a Python role.",
                score=Decimal("5.0"),
                ratings={"cv_match": 5, "role_alignment": 5, "comp": 5, "culture": 5, "red_flags": []},
                job_hash=digest(facts_text(facts)),
                profile_hash=profile.digest,
                model="claude-haiku-5-5",
                prompt_version=PROMPT_VERSION,
            )
        )
    session.flush()


def _listed(session: Session, applicant: uuid.UUID) -> set[int]:
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1, show="all")
    return {n for n in HIDDEN | SHOWN if _job(session, n).id in {listing.job_id for listing in listings}}


def _reported(session: Session) -> set[int]:
    job_ids = set(session.scalars(select(JobReport.job_id)))
    return {n for n in HIDDEN | SHOWN if _job(session, n).id in job_ids}


async def test_d46_a_fits_verdict_of_5_lists_and_reports_only_entry_level_jobs(session: Session) -> None:
    applicant = await _entry_level_person(session)
    _ai_says_every_job_fits(session, applicant)
    ai = _Ai()

    await write_reports(
        session,
        gateway=ai.gateway(),
        settings=_settings(ai_report_unit_price=Decimal("0.0015")),
        persist=session.flush,
        now=SOON,
    )
    match_all(session, now=SOON)

    assert _listed(session, applicant) == set(SHOWN)
    assert _reported(session) == set(SHOWN)


async def test_d46_the_judge_is_not_paid_to_score_internships_or_apprenticeships(session: Session) -> None:
    applicant = await _entry_level_person(session)
    ai = _Ai(judge_score=5.0)

    await _run(session, ai)

    judged = "\n".join(ai.judged)
    assert [extra for _, extra in HIDDEN.values() if extra in judged] == []
    assert _listed(session, applicant) == set(SHOWN)
    assert _reported(session) == set(SHOWN)


async def test_d46_a_report_written_before_the_gate_tailors_no_cv_for_a_hidden_job(session: Session) -> None:
    applicant = await _entry_level_person(session)
    _ai_says_every_job_fits(session, applicant)
    for n in HIDDEN | SHOWN:
        session.add(
            JobReport(
                applicant_id=applicant,
                job_id=_job(session, n).id,
                verdict="Consider",
                score=Decimal("4.2"),
                report=_report(score=4.2, verdict="Consider"),
                input_hash="written before the gate",
                model="claude-haiku-5-5",
                prompt_version=REPORT_PROMPT_VERSION,
            )
        )
    session.flush()
    match_all(session, now=SOON)
    ai = _TailorAi()

    await write_tailored_cvs(
        session,
        gateway=ai.gateway(),
        settings=_settings(ai_tailor_unit_price=Decimal("0.003")),
        persist=session.flush,
        now=SOON,
    )

    tailored = set(session.scalars(select(TailoredCv.job_id)))
    assert tailored == {_job(session, n).id for n in SHOWN}
