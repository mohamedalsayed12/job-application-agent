"""Stage 10: match stored jobs to every applicant, on role, location and salary status.

Rules only: no fetching and no AI, so matching costs nothing and any number of
applicants can share one collection of each board (T27).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.applicants.salary import job_excluded_for_salary, salary_match_status
from app.applicants.types import ExpectedSalary
from app.cleaning.normalize import Place, parse_location, parse_places
from app.matching.entry_level import (
    ENTRY_TITLE,
    NOT_ENTRY_TITLE,
    UK_PLACE,
    YEARS_REQUIRED,
)
from app.models import (
    ApplicantJobMatch,
    ApplicantPreference,
    Job,
    JobFreshness,
    SalaryStatus,
)
from app.quality.checks import Confidence, JobFacts, JobQuality
from app.quality.report import check_all

_TRAINING_TITLE = re.compile(r"\b(intern|interns|internship|placement|apprentice|apprenticeship)\b", re.IGNORECASE)
_SPELLINGS = (
    (re.compile(r"\bback[- ]end\b"), "backend"),
    (re.compile(r"\bfront[- ]end\b"), "frontend"),
    (re.compile(r"\bfull[- ]stack\b"), "fullstack"),
    (re.compile(r"\bserver[- ]side\b"), "backend"),
    (re.compile(r"\b(?:developer|programmer|engineering|development|dev|swe)\b"), "engineer"),
)
_LEVEL_WORDS = frozenset(
    "graduate grad new junior jr entry level early career senior snr sr staff principal lead".split()
)
_ENGINEER = "engineer"
# Titles that share a skill with the target. Milestone 2 replaces this table with skill overlap.
_SIMILAR_TITLES = {
    "backend": frozenset({"software", "python", "fullstack", "platform", "api"}),
    "python": frozenset({"software", "backend", "fullstack", "api", "django", "platform"}),
    "software": frozenset(
        {"backend", "frontend", "fullstack", "python", "platform", "web", "java", "golang", "api", "applications"}
    ),
    "fullstack": frozenset({"software", "backend", "frontend", "web", "python"}),
    "frontend": frozenset({"web", "fullstack", "software"}),
    "data": frozenset({"analytics", "etl"}),
}
# A title with one of these is a different job, unless the applicant asked for that word.
_DIFFERENT_JOB_WORDS = frozenset(
    {"sales", "solutions", "support", "hardware", "field", "manager", "director", "head", "vp", "recruiter", "scheduler"}
)
_EUROPE = frozenset(
    "GB IE FR DE ES IT NL PT PL LV LT EE BE BG RO SE NO DK FI CH AT LU GR CZ HU RS HR SK SI UA CY MT IS".split()
)
_REGION_COUNTRIES = {
    "europe": _EUROPE,
    "emea": _EUROPE | frozenset("AE SA QA KW TR IL EG ZA NG KE".split()),
    "americas": frozenset("US CA MX BR AR CO CL".split()),
    "apac": frozenset("IN SG CN TW AU JP PK NZ KR HK MY TH ID PH VN".split()),
}

_ROLE_SCORE = {"exact": Decimal("0.5"), "similar": Decimal("0.3")}
_ENTRY_SCORE = Decimal("0.1")
_LOCATION_SCORE = Decimal("0.3")
_SALARY_MEETS_SCORE = Decimal("0.1")


class RoleKind(StrEnum):
    exact = "exact"
    similar = "similar"


@dataclass(frozen=True, slots=True)
class RoleMatch:
    target: str
    kind: RoleKind
    level: str | None


@dataclass(frozen=True, slots=True)
class Wanted:
    """One location an applicant asked for."""

    city: str | None
    country: str | None
    remote: bool


@dataclass(slots=True)
class MatchReport:
    applicants: int = 0
    matches: int = 0
    removed: int = 0
    by_applicant: dict[uuid.UUID, int] = field(default_factory=dict)


def _title_words(text: str) -> set[str]:
    lowered = text.lower()
    for pattern, spelling in _SPELLINGS:
        lowered = pattern.sub(spelling, lowered)
    return set(re.findall(r"[a-z0-9+#]+", lowered))


def match_role(title: str, description: str | None, target_roles: list[str]) -> RoleMatch | None:
    """The best way this job fits any target role. An exact fit beats a similar one."""
    words = _title_words(title)
    found = [fit for role in target_roles if (fit := _fit_role(title, words, description, role))]
    return min(found, key=lambda fit: fit.kind != RoleKind.exact) if found else None


def _fit_role(title: str, words: set[str], description: str | None, role: str) -> RoleMatch | None:
    wants_entry = bool(ENTRY_TITLE.search(role))
    if _TRAINING_TITLE.search(title) and not _TRAINING_TITLE.search(role):
        return None
    says_entry = bool(ENTRY_TITLE.search(title))
    if wants_entry and NOT_ENTRY_TITLE.search(title):
        return None
    if wants_entry and not says_entry and YEARS_REQUIRED.search(description or ""):
        return None

    core = _title_words(role) - _LEVEL_WORDS
    if not core or words & (_DIFFERENT_JOB_WORDS - core):
        return None
    level = None
    if wants_entry:
        level = "entry level in the title" if says_entry else "level not stated"
    if core <= words:
        return RoleMatch(role, RoleKind.exact, level)
    if _ENGINEER in core and _ENGINEER in words:
        skills = core - {_ENGINEER}
        if any(_SIMILAR_TITLES.get(skill, frozenset()) & words for skill in skills):
            return RoleMatch(role, RoleKind.similar, level)
    return None


def wanted_locations(locations: list[str]) -> list[Wanted]:
    wanted = []
    for text in locations:
        remote = parse_location(text).remote_type == "remote"
        places = parse_places(text) or (Place(),)
        for place in places:
            country = place.country or ("GB" if place.city and UK_PLACE.search(place.city) else None)
            wanted.append(Wanted(place.city, country, remote))
    return wanted


def location_fit(wanted: list[Wanted], places: list[dict[str, Any]], remote_type: str | None) -> tuple[bool, str]:
    """True with the reason it fits. Different named countries never fit, even for the same city name."""
    for want in wanted:
        if want.remote:
            continue
        for place in places:
            if _same_city(want, place):
                return True, f"in {place['city']}"
        for place in places:
            if remote_type != "remote" and place["city"] is None and _covers(place, want.country):
                return True, f"anywhere in {place['country'] or place['region']}"
    if remote_type == "remote":
        for want in wanted:
            if not places:
                return True, "remote, country not stated"
            for place in places:
                if _covers(place, want.country):
                    return True, f"remote ({place['country'] or place['region']})"
            if want.country is None and want.remote:
                return True, "remote"
    return False, "not in the applicant's locations"


def _covers(place: dict[str, Any], country: str | None) -> bool:
    """Whether this place includes the country. Worldwide includes everywhere, known or not."""
    region = place.get("region")
    if region == "worldwide":
        return True
    if country is None:
        return False
    return place["country"] == country or country in _REGION_COUNTRIES.get(region, frozenset())


def _same_city(want: Wanted, place: dict[str, Any]) -> bool:
    if not want.city or not place["city"] or want.city.lower() != place["city"].lower():
        return False
    return not (want.country and place["country"]) or want.country == place["country"]


def _score(role: RoleMatch, fits: bool, salary: SalaryStatus) -> Decimal:
    score = _ROLE_SCORE[role.kind]
    if role.level == "entry level in the title":
        score += _ENTRY_SCORE
    if fits:
        score += _LOCATION_SCORE
    if salary is SalaryStatus.meets:
        score += _SALARY_MEETS_SCORE
    return score


def _reasons(role: RoleMatch, location: str, fits: bool, salary: SalaryStatus) -> dict[str, Any]:
    missing = []
    if not fits:
        missing.append("location")
    if role.kind == RoleKind.similar:
        missing.append("exact role title")
    if role.level == "level not stated":
        missing.append("stated entry level")
    if salary is SalaryStatus.unknown:
        missing.append("known salary")
    return {
        "role": {"target": role.target, "kind": role.kind, "level": role.level},
        "location": location,
        "salary": salary,
        "missing": missing,
    }


def _expected_salary(preference: ApplicantPreference) -> ExpectedSalary | None:
    if preference.salary_amount is None or not preference.salary_currency or not preference.salary_period:
        return None
    return ExpectedSalary(preference.salary_amount, preference.salary_currency, preference.salary_period)


def _can_be_shown(facts: JobFacts, quality: JobQuality) -> bool:
    """D23: open jobs only, so `unknown` jobs stay out until they are confirmed open again."""
    return (
        facts.freshness == JobFreshness.open
        and quality.recent
        and quality.unique
        and quality.confidence != Confidence.low
    )


def match_all(session: Session, *, now: datetime) -> MatchReport:
    """Safe to repeat. Afterwards the table holds exactly the current matches."""
    report = MatchReport()
    shown = [facts for facts, quality in check_all(session, now) if _can_be_shown(facts, quality)]
    for preference in session.scalars(select(ApplicantPreference)).all():
        report.applicants += 1
        _match_applicant(session, preference, shown, report)
    session.flush()
    return report


def _match_applicant(
    session: Session, preference: ApplicantPreference, shown: list[JobFacts], report: MatchReport
) -> None:
    applicant_id = preference.applicant_id
    wanted = wanted_locations(preference.locations)
    expected = _expected_salary(preference)
    existing = {
        row.job_id: row
        for row in session.scalars(select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant_id))
    }
    kept: set[uuid.UUID] = set()
    for facts in shown:
        if facts.sector != preference.sector:
            continue
        role = match_role(facts.title, facts.description, preference.target_roles)
        if role is None:
            continue
        job = session.get(Job, facts.job_id)
        salary = salary_match_status(
            expected,
            job_salary_known=job.salary_known,
            job_min=job.salary_min,
            job_max=job.salary_max,
            job_currency=job.salary_currency,
            job_period=job.salary_period,
        )
        if job_excluded_for_salary(salary):
            continue
        fits, location = location_fit(wanted, facts.places, facts.remote_type)
        row = existing.get(facts.job_id)
        if row is None:
            row = ApplicantJobMatch(applicant_id=applicant_id, job_id=facts.job_id)
            session.add(row)
        row.score = _score(role, fits, salary)
        row.reasons = _reasons(role, location, fits, salary)
        row.role_match = True
        row.location_fit = fits
        row.salary_status = salary
        kept.add(facts.job_id)

    stale = set(existing) - kept
    if stale:
        session.execute(
            delete(ApplicantJobMatch).where(
                ApplicantJobMatch.applicant_id == applicant_id, ApplicantJobMatch.job_id.in_(stale)
            )
        )
    report.removed += len(stale)
    report.matches += len(kept)
    report.by_applicant[applicant_id] = len(kept)
