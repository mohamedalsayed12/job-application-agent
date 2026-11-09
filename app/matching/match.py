"""Stage 10: match stored jobs to every applicant, on role, location, salary status, skills and licences.

Rules only: no fetching and no AI, so matching costs nothing and any number of
applicants can share one collection of each board (T27).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
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
from app.matching.credentials import Held, held_groups, required_in_advert
from app.matching.entry_level import (
    ENTRY_TITLE,
    NOT_ENTRY_TITLE,
    UK_PLACE,
    YEARS_REQUIRED,
)
from app.models import (
    ApplicantCertification,
    ApplicantJobMatch,
    ApplicantPreference,
    ApplicantProfile,
    ApplicantSkill,
    ExperienceLevel,
    Job,
    JobFreshness,
    JobSkill,
    SalaryStatus,
    Skill,
)
from app.quality.checks import Confidence, JobFacts, JobQuality
from app.quality.report import check_all
from app.skills.mapping import label_forms

_TRAINING_TITLE = re.compile(r"\b(intern|interns|internship|placement|apprentice|apprenticeship)\b", re.IGNORECASE)
_INTERNSHIP_TITLE = re.compile(r"\b(intern|interns|internship|internships|placement|placements)\b", re.IGNORECASE)
_APPRENTICE_TITLE = re.compile(r"\b(apprentice|apprentices|apprenticeship|apprenticeships)\b", re.IGNORECASE)
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
_INTERNSHIP_WORDS = frozenset("intern interns internship internships placement placements".split())
_INTERNSHIP_LEVEL = "internship in the title"
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
_SKILLS_SCORE = Decimal("0.3")
_MISSING_LICENCE_COST = Decimal("0.2")
_SCORE_PLACES = Decimal("0.001")


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


@dataclass(frozen=True, slots=True)
class SkillFit:
    matched: list[str]
    missing: list[str]
    on_profile: bool

    @property
    def share(self) -> Decimal:
        named = len(self.matched) + len(self.missing)
        return Decimal(len(self.matched)) / named if named else Decimal(0)


@dataclass(frozen=True, slots=True)
class Fit:
    """Everything one match is scored on, so the score and the reasons can't disagree."""

    role: RoleMatch
    location: str
    location_fits: bool
    salary: SalaryStatus
    skills: SkillFit
    missing_licences: list[str]


@dataclass(slots=True)
class Skills:
    """Job and applicant skills, loaded once per run."""

    names: dict[uuid.UUID, str]
    by_job: dict[uuid.UUID, set[uuid.UUID]]
    by_applicant: dict[uuid.UUID, set[uuid.UUID]]
    credentials: dict[uuid.UUID, set[str]]

    def fit(self, applicant_id: uuid.UUID, job_id: uuid.UUID) -> SkillFit:
        held = self.by_applicant.get(applicant_id, set())
        named = self.by_job.get(job_id, set())
        return SkillFit(self._sorted(named & held), self._sorted(named - held), bool(held))

    def _sorted(self, ids: set[uuid.UUID]) -> list[str]:
        return sorted((self.names[skill_id] for skill_id in ids), key=str.casefold)


def load_skills(session: Session, *, now: datetime) -> Skills:
    names = {skill_id: label_forms(label)[-1] for skill_id, label in session.execute(select(Skill.id, Skill.preferred_label))}
    by_job: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for job_id, skill_id in session.execute(select(JobSkill.job_id, JobSkill.skill_id)):
        by_job[job_id].add(skill_id)
    by_applicant: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for applicant_id, skill_id in session.execute(
        select(ApplicantSkill.applicant_id, ApplicantSkill.skill_id).where(ApplicantSkill.skill_id.is_not(None))
    ):
        by_applicant[applicant_id].add(skill_id)
    held: dict[uuid.UUID, list[Held]] = defaultdict(list)
    for row in session.scalars(select(ApplicantCertification)):
        held[row.applicant_id].append(Held(row.name, row.issuer, row.expires_on))
    credentials = {applicant_id: held_groups(items, today=now.date()) for applicant_id, items in held.items()}
    return Skills(names, by_job, by_applicant, credentials)


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


def match_role(
    title: str, description: str | None, target_roles: list[str], *, internship: bool = False
) -> RoleMatch | None:
    """The best way this job fits any target role. An exact fit beats a similar one.

    `internship` is for someone whose profile says they want one: they see only internships.
    """
    words = _title_words(title)
    fit_one = _fit_internship if internship else _fit_role
    found = [fit for role in target_roles if (fit := fit_one(title, words, description, role))]
    return min(found, key=lambda fit: fit.kind != RoleKind.exact) if found else None


def _fit_internship(title: str, words: set[str], _description: str | None, role: str) -> RoleMatch | None:
    if not _INTERNSHIP_TITLE.search(title) or _APPRENTICE_TITLE.search(title):
        return None
    return _fit_title(words, _title_words(role) - _LEVEL_WORDS - _INTERNSHIP_WORDS, role, _INTERNSHIP_LEVEL)


def _fit_role(title: str, words: set[str], description: str | None, role: str) -> RoleMatch | None:
    wants_entry = bool(ENTRY_TITLE.search(role))
    if _TRAINING_TITLE.search(title) and not _TRAINING_TITLE.search(role):
        return None
    says_entry = bool(ENTRY_TITLE.search(title))
    if wants_entry and NOT_ENTRY_TITLE.search(title):
        return None
    if wants_entry and not says_entry and YEARS_REQUIRED.search(description or ""):
        return None

    level = None
    if wants_entry:
        level = "entry level in the title" if says_entry else "level not stated"
    return _fit_title(words, _title_words(role) - _LEVEL_WORDS, role, level)


def _fit_title(words: set[str], core: set[str], role: str, level: str | None) -> RoleMatch | None:
    if not core or words & (_DIFFERENT_JOB_WORDS - core):
        return None
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


def score(fit: Fit) -> Decimal:
    """Up to 1.3. Skills add up to 0.3 by the share of the advert's skills the applicant has."""
    total = _ROLE_SCORE[fit.role.kind]
    if fit.role.level in ("entry level in the title", _INTERNSHIP_LEVEL):
        total += _ENTRY_SCORE
    if fit.location_fits:
        total += _LOCATION_SCORE
    if fit.salary is SalaryStatus.meets:
        total += _SALARY_MEETS_SCORE
    total += _SKILLS_SCORE * fit.skills.share
    total -= _MISSING_LICENCE_COST * len(fit.missing_licences)
    return max(total, Decimal(0)).quantize(_SCORE_PLACES)


def reasons(fit: Fit) -> dict[str, Any]:
    missing = []
    if not fit.location_fits:
        missing.append("location")
    if fit.role.kind == RoleKind.similar:
        missing.append("exact role title")
    if fit.role.level == "level not stated":
        missing.append("stated entry level")
    if fit.salary is SalaryStatus.unknown:
        missing.append("known salary")
    if not fit.skills.on_profile:
        missing.append("skills on profile")
    missing += [f"skill: {name}" for name in fit.skills.missing]
    missing += [f"licence: {name}" for name in fit.missing_licences]
    return {
        "role": {"target": fit.role.target, "kind": fit.role.kind, "level": fit.role.level},
        "location": fit.location,
        "salary": fit.salary,
        "skills": {"matched": fit.skills.matched, "missing": fit.skills.missing},
        "missing_licences": fit.missing_licences,
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


def internship_seekers(session: Session) -> set[uuid.UUID]:
    """Applicants whose profile asks for internships. Everyone else never sees one."""
    return set(
        session.scalars(
            select(ApplicantProfile.applicant_id).where(ApplicantProfile.experience_level == ExperienceLevel.internship)
        )
    )


def match_all(session: Session, *, now: datetime) -> MatchReport:
    """Safe to repeat. Afterwards the table holds exactly the current matches."""
    report = MatchReport()
    shown = [facts for facts, quality in check_all(session, now) if _can_be_shown(facts, quality)]
    seekers = internship_seekers(session)
    skills = load_skills(session, now=now)
    required = {facts.job_id: required_in_advert(facts.description) for facts in shown}
    for preference in session.scalars(select(ApplicantPreference)).all():
        report.applicants += 1
        _match_applicant(
            session, preference, shown, report, skills, required, internship=preference.applicant_id in seekers
        )
    session.flush()
    return report


def _match_applicant(
    session: Session,
    preference: ApplicantPreference,
    shown: list[JobFacts],
    report: MatchReport,
    skills: Skills,
    required: dict[uuid.UUID, list[str]],
    *,
    internship: bool,
) -> None:
    applicant_id = preference.applicant_id
    wanted = wanted_locations(preference.locations)
    expected = _expected_salary(preference)
    held = skills.credentials.get(applicant_id, set())
    existing = {
        row.job_id: row
        for row in session.scalars(select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant_id))
    }
    kept: set[uuid.UUID] = set()
    for facts in shown:
        if facts.sector != preference.sector:
            continue
        role = match_role(facts.title, facts.description, preference.target_roles, internship=internship)
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
        fit = Fit(
            role=role,
            location=location,
            location_fits=fits,
            salary=salary,
            skills=skills.fit(applicant_id, facts.job_id),
            missing_licences=[name for name in required[facts.job_id] if name not in held],
        )
        row = existing.get(facts.job_id)
        if row is None:
            row = ApplicantJobMatch(applicant_id=applicant_id, job_id=facts.job_id)
            session.add(row)
        row.score = score(fit)
        row.reasons = reasons(fit)
        row.role_match = True
        row.location_fit = fits
        row.salary_status = salary
        row.matched_skills = fit.skills.matched
        row.missing_skills = fit.skills.missing
        row.missing_licences = fit.missing_licences
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
