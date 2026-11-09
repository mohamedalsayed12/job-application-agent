"""The person's own profile: preferences, experience, travel, skills and certifications (T30)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.applicants.service import (
    PreferenceValidationError,
    parse_preference_input,
    update_applicant,
)
from app.applicants.types import PreferenceInput
from app.models import (
    ApplicantCertification,
    ApplicantPreference,
    ApplicantProfile,
    ApplicantSkill,
    ExperienceLevel,
)
from app.skills.taxonomy import label_index, skill_for_text

EXPERIENCE_LABELS = {
    ExperienceLevel.internship: "Internship",
    ExperienceLevel.entry: "Entry level (graduate or junior)",
    ExperienceLevel.mid: "Mid",
    ExperienceLevel.senior: "Senior",
}
MIN_TRAVEL_MILES = 1
MAX_TRAVEL_MILES = 500
# Roles and locations become search queries, so these also bound what one profile can ask for.
MAX_ROLES = 10
MAX_LOCATIONS = 10
MAX_SKILLS = 50
MAX_CERTIFICATIONS = 20
MAX_ITEM_LENGTH = 200


@dataclass(frozen=True, slots=True)
class CertificationInput:
    name: str
    issuer: str | None
    is_licence: bool
    expires_on: date | None


@dataclass(frozen=True, slots=True)
class ProfileInput:
    preference: PreferenceInput
    experience_level: ExperienceLevel
    travel_distance_miles: int | None
    skills: tuple[str, ...]
    certifications: tuple[CertificationInput, ...]


@dataclass(frozen=True, slots=True)
class SavedProfile:
    preference: ApplicantPreference | None
    profile: ApplicantProfile | None
    skills: list[str]
    certifications: list[ApplicantCertification]


def _check_items(items: list[str], *, label: str, limit: int) -> None:
    if len(items) > limit:
        raise PreferenceValidationError(f"Enter at most {limit} {label}.")
    if any(len(item) > MAX_ITEM_LENGTH for item in items):
        raise PreferenceValidationError(f"Keep each of your {label} under {MAX_ITEM_LENGTH} characters.")


def _parse_level(value: str) -> ExperienceLevel:
    try:
        return ExperienceLevel(value)
    except ValueError as exc:
        raise PreferenceValidationError("Choose an experience level.") from exc


def _parse_travel(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.isdecimal() or not MIN_TRAVEL_MILES <= int(cleaned) <= MAX_TRAVEL_MILES:
        raise PreferenceValidationError(
            f"Travel distance must be a whole number of miles from {MIN_TRAVEL_MILES} to {MAX_TRAVEL_MILES}, or blank."
        )
    return int(cleaned)


def _parse_skills(skills: list[str]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for skill in skills:
        cleaned = skill.strip()
        if cleaned:
            unique.setdefault(cleaned.casefold(), cleaned)
    kept = list(unique.values())
    _check_items(kept, label="skills", limit=MAX_SKILLS)
    return tuple(kept)


def parse_certification(name: str, issuer: str, kind: str, expires_on: str) -> CertificationInput | None:
    """A row with nothing typed in it is one of the form's spare rows, so it is skipped."""
    name, issuer, expires_on = name.strip(), issuer.strip(), expires_on.strip()
    if not (name or issuer or expires_on):
        return None
    if not name:
        raise PreferenceValidationError("Each certification or licence needs a name.")
    if len(name) > MAX_ITEM_LENGTH or len(issuer) > MAX_ITEM_LENGTH:
        raise PreferenceValidationError(f"Keep certification details under {MAX_ITEM_LENGTH} characters.")
    try:
        expiry = date.fromisoformat(expires_on) if expires_on else None
    except ValueError as exc:
        raise PreferenceValidationError(f"The expiry date for {name} isn't a valid date.") from exc
    return CertificationInput(name, issuer or None, kind == "licence", expiry)


def parse_profile_input(
    *,
    target_roles: list[str],
    locations: list[str],
    salary_amount: str,
    salary_currency: str,
    salary_period: str,
    experience_level: str,
    travel_distance: str,
    skills: list[str],
    certifications: list[CertificationInput],
) -> ProfileInput:
    roles = [role.strip() for role in target_roles if role.strip()]
    places = [place.strip() for place in locations if place.strip()]
    _check_items(roles, label="target roles", limit=MAX_ROLES)
    _check_items(places, label="locations", limit=MAX_LOCATIONS)
    if len(certifications) > MAX_CERTIFICATIONS:
        raise PreferenceValidationError(f"Enter at most {MAX_CERTIFICATIONS} certifications and licences.")
    try:
        preference = parse_preference_input(
            target_roles=roles,
            locations=places,
            salary_amount=salary_amount,
            salary_currency=salary_currency,
            salary_period=salary_period,
        )
    except InvalidOperation as exc:
        raise PreferenceValidationError("Salary amount must be a number.") from exc
    return ProfileInput(
        preference=preference,
        experience_level=_parse_level(experience_level),
        travel_distance_miles=_parse_travel(travel_distance),
        skills=_parse_skills(skills),
        certifications=tuple(certifications),
    )


def save_profile(session: Session, applicant_id: uuid.UUID, profile: ProfileInput) -> None:
    update_applicant(session, applicant_id, preference=profile.preference)
    row = session.scalar(select(ApplicantProfile).where(ApplicantProfile.applicant_id == applicant_id))
    if row is None:
        row = ApplicantProfile(applicant_id=applicant_id)
        session.add(row)
    row.experience_level = profile.experience_level
    row.travel_distance_miles = profile.travel_distance_miles

    session.execute(delete(ApplicantSkill).where(ApplicantSkill.applicant_id == applicant_id))
    session.execute(delete(ApplicantCertification).where(ApplicantCertification.applicant_id == applicant_id))
    index = label_index(session)
    session.add_all(
        ApplicantSkill(applicant_id=applicant_id, text_as_typed=skill, skill_id=skill_for_text(index, skill))
        for skill in profile.skills
    )
    session.add_all(
        ApplicantCertification(
            applicant_id=applicant_id,
            name=item.name,
            issuer=item.issuer,
            is_licence=item.is_licence,
            expires_on=item.expires_on,
        )
        for item in profile.certifications
    )
    session.flush()


def load_profile(session: Session, applicant_id: uuid.UUID) -> SavedProfile:
    return SavedProfile(
        preference=session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id)),
        profile=session.scalar(select(ApplicantProfile).where(ApplicantProfile.applicant_id == applicant_id)),
        skills=list(
            session.scalars(
                select(ApplicantSkill.text_as_typed)
                .where(ApplicantSkill.applicant_id == applicant_id)
                .order_by(ApplicantSkill.created_at, ApplicantSkill.text_as_typed)
            )
        ),
        certifications=list(
            session.scalars(
                select(ApplicantCertification)
                .where(ApplicantCertification.applicant_id == applicant_id)
                .order_by(ApplicantCertification.created_at, ApplicantCertification.name)
            )
        ),
    )
