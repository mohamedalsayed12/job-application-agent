"""Which of the AI's job verdicts still apply (D50). Shared by the judge and by matching.

A verdict holds while the job's text and the person's profile are what the AI read. Location and
salary are left out of the profile: code checks those, so changing them never costs a new verdict.
Right to work and visa sponsorship (D79) are included when ApplicationAnswers has them, so a change
there refreshes the judge and the fit report.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.levels import Levels, chosen_or_inferred, in_order, parse_levels
from app.models import (
    ApplicantPreference,
    ApplicantProfile,
    ApplicantSkill,
    ApplicationAnswers,
    JobReport,
    JobVerdict,
)

PROMPT_VERSION = "job-judge-v4"
# D52: the full fit report. Shared here so matching can read reports without importing the writer.
REPORT_PROMPT_VERSION = "fit-report-v1"
# Operator's choice (2026-11-10): career-ops draws its apply line at 4.0; this product adds jobs from 3.0.
ADD_LINE = Decimal("3.0")
DESCRIPTION_CHARS = 6000


@dataclass(frozen=True, slots=True)
class Profile:
    applicant_id: uuid.UUID
    sector: str
    text: str
    digest: str
    roles: tuple[str, ...] = ()
    levels: Levels = frozenset()
    right_to_work_uk: bool | None = None
    needs_visa_sponsorship: bool | None = None


def job_text(
    *,
    title: str,
    company: str,
    places: Iterable[dict[str, Any]],
    apply_url: str | None,
    description: str | None,
) -> str:
    where = ", ".join(place.get("city") or place.get("country") or place.get("region") or "" for place in places)
    return (
        f"Title: {title}\nEmployer: {company}\nLocation: {where}\nLink: {apply_url or ''}\n\n"
        f"{(description or '')[:DESCRIPTION_CHARS]}"
    )


def digest(text: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\n{text}".encode()).hexdigest()


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "not given"
    return "yes" if value else "no"


def _visa_lines(*, right_to_work_uk: bool | None, needs_visa_sponsorship: bool | None) -> str:
    """Blank when neither answer is set, so people without apply answers keep their old digest shape."""
    if right_to_work_uk is None and needs_visa_sponsorship is None:
        return ""
    return (
        f"\nRight to work in the UK: {_yes_no(right_to_work_uk)}\n"
        f"Needs visa sponsorship: {_yes_no(needs_visa_sponsorship)}"
    )


def load_profiles(session: Session, applicant_ids: set[uuid.UUID] | None = None) -> dict[uuid.UUID, Profile]:
    """What the AI is told about each person who has saved preferences."""
    preferences = select(ApplicantPreference)
    if applicant_ids is not None:
        preferences = preferences.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    profiles = {row.applicant_id: row for row in session.scalars(select(ApplicantProfile))}
    answers_q = select(ApplicationAnswers)
    if applicant_ids is not None:
        answers_q = answers_q.where(ApplicationAnswers.applicant_id.in_(applicant_ids))
    answers = {row.applicant_id: row for row in session.scalars(answers_q)}
    skills: dict[uuid.UUID, list[str]] = {}
    for applicant_id, name in session.execute(select(ApplicantSkill.applicant_id, ApplicantSkill.text_as_typed)):
        skills.setdefault(applicant_id, []).append(str(name))
    out: dict[uuid.UUID, Profile] = {}
    for preference in session.scalars(preferences):
        profile = profiles.get(preference.applicant_id)
        apply = answers.get(preference.applicant_id)
        right_to_work = apply.right_to_work_uk if apply else None
        needs_sponsorship = apply.needs_visa_sponsorship if apply else None
        text = (
            f"Target roles: {', '.join(preference.target_roles)}\n"
            f"Experience levels wanted: {', '.join(in_order(profile.experience_levels)) if profile else 'not given'}\n"
            f"Skills: {', '.join(sorted(skills.get(preference.applicant_id, []), key=str.lower)) or 'none given'}\n"
            f"Degrees: {', '.join(profile.degrees) if profile and profile.degrees else 'none given'}\n"
            f"Sector: {preference.sector}"
            f"{_visa_lines(right_to_work_uk=right_to_work, needs_visa_sponsorship=needs_sponsorship)}"
        )
        out[preference.applicant_id] = Profile(
            preference.applicant_id,
            preference.sector,
            text,
            digest(text),
            roles=tuple(preference.target_roles),
            levels=chosen_or_inferred(parse_levels(profile.experience_levels) if profile else frozenset(), preference.target_roles),
            right_to_work_uk=right_to_work,
            needs_visa_sponsorship=needs_sponsorship,
        )
    return out


def current_verdicts(
    session: Session, profile: Profile, job_digests: dict[uuid.UUID, str]
) -> dict[uuid.UUID, JobVerdict]:
    """This person's verdicts that were made from the job text and profile as they are now."""
    rows = session.scalars(select(JobVerdict).where(JobVerdict.applicant_id == profile.applicant_id))
    return {
        row.job_id: row
        for row in rows
        if row.profile_hash == profile.digest and row.job_hash == job_digests.get(row.job_id)
    }


def current_reports(session: Session, applicant_id: uuid.UUID) -> dict[uuid.UUID, JobReport]:
    """This person's fit reports from the current prompt, by job (D52)."""
    rows = session.scalars(
        select(JobReport).where(JobReport.applicant_id == applicant_id, JobReport.prompt_version == REPORT_PROMPT_VERSION)
    )
    return {row.job_id: row for row in rows}
