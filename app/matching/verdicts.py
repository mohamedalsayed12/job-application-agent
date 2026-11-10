"""Which of the AI's job verdicts still apply (D50). Shared by the judge and by matching.

A verdict holds while the job's text and the person's profile are what the AI read. Location and
salary are left out of the profile: code checks those, so changing them never costs a new verdict.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApplicantPreference, ApplicantProfile, ApplicantSkill, JobVerdict

PROMPT_VERSION = "job-judge-v2"
DESCRIPTION_CHARS = 6000


@dataclass(frozen=True, slots=True)
class Profile:
    applicant_id: uuid.UUID
    sector: str
    text: str
    digest: str
    roles: tuple[str, ...] = ()


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


def load_profiles(session: Session, applicant_ids: set[uuid.UUID] | None = None) -> dict[uuid.UUID, Profile]:
    """What the AI is told about each person who has saved preferences."""
    preferences = select(ApplicantPreference)
    if applicant_ids is not None:
        preferences = preferences.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    profiles = {row.applicant_id: row for row in session.scalars(select(ApplicantProfile))}
    skills: dict[uuid.UUID, list[str]] = {}
    for applicant_id, name in session.execute(select(ApplicantSkill.applicant_id, ApplicantSkill.text_as_typed)):
        skills.setdefault(applicant_id, []).append(str(name))
    out: dict[uuid.UUID, Profile] = {}
    for preference in session.scalars(preferences):
        profile = profiles.get(preference.applicant_id)
        text = (
            f"Target roles: {', '.join(preference.target_roles)}\n"
            f"Experience level: {profile.experience_level if profile else 'not given'}\n"
            f"Skills: {', '.join(sorted(skills.get(preference.applicant_id, []), key=str.lower)) or 'none given'}\n"
            f"Degrees: {', '.join(profile.degrees) if profile and profile.degrees else 'none given'}\n"
            f"Sector: {preference.sector}"
        )
        out[preference.applicant_id] = Profile(
            preference.applicant_id,
            preference.sector,
            text,
            digest(text),
            roles=tuple(preference.target_roles),
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
