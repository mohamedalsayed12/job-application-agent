"""Stage 10: match stored jobs to every applicant, on role, location, salary status, skills and licences.

Rules, plus the AI's saved verdicts (D50): a job the AI says fits is matched even when the rules
miss it, and a job it says doesn't fit is left out. Matching itself never calls the AI, so it
costs nothing and any number of applicants can share one collection of each board (T27).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.applicants.levels import LEVELS, Levels, chosen_or_inferred, saved_levels
from app.applicants.salary import job_excluded_for_salary, salary_match_status
from app.applicants.types import ExpectedSalary
from app.cleaning.normalize import Place, parse_location, parse_places
from app.matching.credentials import Held, held_groups, required_in_advert
from app.matching import semantic
from app.matching.related_titles import with_related
from app.matching.semantic import Embedder, similarity
from app.matching.titles import (
    ENTRY_TITLE,
    NOT_ENTRY_TITLE,
    UK_PLACE,
    YEARS_REQUIRED,
)
from app.matching.verdicts import (
    ADD_LINE,
    Profile,
    current_reports,
    current_verdicts,
    digest,
    job_text,
    load_profiles,
)
from app.models import (
    ApplicantCertification,
    ApplicantJobMatch,
    ApplicantPreference,
    ApplicantSkill,
    ExperienceLevel,
    Job,
    JobFreshness,
    JobSkill,
    JobVerdict,
    SalaryStatus,
    Skill,
)
from app.quality.checks import Confidence, JobFacts, JobQuality
from app.quality.report import check_all
from app.skills.mapping import label_forms

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
# How close in meaning a job title must be to one of the person's titles to match it. Set on 248 stored job
# titles: the lowest line at which no title from another kind of job matched (see MVP.md).
TITLE_SIMILAR = 0.82
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

# D50: the AI judged the job against the person's roles, level and skills, so it scores between exact and similar.
_ROLE_SCORE = {"exact": Decimal("0.5"), "ai": Decimal("0.4"), "similar": Decimal("0.3"), "skills": Decimal("0.2")}
_ENTRY_SCORE = Decimal("0.1")
_LOCATION_SCORE = Decimal("0.3")
_SALARY_MEETS_SCORE = Decimal("0.1")
_SKILLS_SCORE = Decimal("0.3")
_MISSING_LICENCE_COST = Decimal("0.2")
_SCORE_PLACES = Decimal("0.001")
# D44: a job outside the target roles matches when the applicant has most of the skills its advert names.
SKILLS_MATCH_MIN = 3
SKILLS_MATCH_SHARE = Decimal("0.5")
ENTRY_IN_TITLE = "entry level in the title"
ENTRY_IN_ADVERT = "entry level in the advert"
ENTRY_BY_AI = "entry level, judged by AI"
# D46: wording that shows an untitled job is for people starting out. A senior advert that
# mentions graduates ("mentor new graduates") usually also asks for 3+ years, which rules it out first.
_ENTRY_ADVERT = re.compile(
    r"\b(?:entry[- ]level|(?:recent|new) grad(?:uate)?s?|graduate (?:scheme|programme|program|intake|role|position|"
    r"opportunit\w+)|early[- ]careers?|(?:no|little) (?:prior |previous |commercial |professional |industry )?"
    r"experience (?:is )?(?:required|needed|necessary)|(?:0|1|zero|one) ?(?:-|–|to) ?(?:1|2|two) (?:years?|yrs?)|"
    r"up to (?:1|2|two) years?)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\bnot (?:an? |for )?$", re.IGNORECASE)


class RoleKind(StrEnum):
    exact = "exact"
    similar = "similar"
    skills = "skills"
    ai = "ai"


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


class TitleMeaning:
    """Compares job titles by meaning. Each title's vector is made once, and the model loads on first use."""

    def __init__(self, load: Callable[[], Embedder]) -> None:
        self._load = load
        self._embedder: Embedder | None = None
        self._vectors: dict[str, list[float]] = {}

    def _vector(self, title: str) -> list[float]:
        key = " ".join(title.casefold().split())
        if key not in self._vectors:
            self._embedder = self._embedder or self._load()
            self._vectors[key] = self._embedder.passages([key])[0]
        return self._vectors[key]

    def closest(self, title: str, roles: list[str]) -> str | None:
        """The role nearest in meaning to the job title, when it is near enough."""
        if not roles:
            return None
        job = self._vector(title)
        nearness, role = max((similarity(job, self._vector(role)), role) for role in roles)
        return role if nearness >= TITLE_SIMILAR else None


def _title_words(text: str) -> set[str]:
    lowered = text.lower()
    for pattern, spelling in _SPELLINGS:
        lowered = pattern.sub(spelling, lowered)
    return set(re.findall(r"[a-z0-9+#]+", lowered))


def _core(roles: list[str]) -> set[str]:
    return set().union(*(_title_words(role) for role in roles)) - _LEVEL_WORDS - _INTERNSHIP_WORDS


def worth_judging(title: str, titles: list[str], levels: Levels, meaning: TitleMeaning) -> bool:
    """A free check before paying the AI (D50): the job title shares a word with one of the person's titles,
    their target roles and the titles related to them, or is close to one in meaning.

    A job whose title shuts this person out (D63) is never shown to them, so it is never worth paying for.
    """
    if training_title_shuts_out(title, levels):
        return False
    words, core = _title_words(title), _core(titles)
    if words & (_DIFFERENT_JOB_WORDS - core):
        return False
    return bool(words & core) or meaning.closest(title, titles) is not None


def training_title_shuts_out(title: str, levels: Levels) -> bool:
    """D63: apprenticeships are shown to nobody, and internships only to people who chose them."""
    if _APPRENTICE_TITLE.search(title):
        return True
    return bool(_INTERNSHIP_TITLE.search(title)) and ExperienceLevel.internship not in levels


def level_fit(title: str, description: str | None, levels: Levels) -> tuple[bool, str | None]:
    """Whether the job is at a level the person chose, and what shows it.

    Someone who chose only entry level sees only jobs shown to be entry level (D46). Anyone who
    also chose mid or senior sees every ordinary job.
    """
    if training_title_shuts_out(title, levels):
        return False, None
    if _INTERNSHIP_TITLE.search(title):
        return True, _INTERNSHIP_LEVEL
    ordinary = [LEVELS[level] for level in levels - {ExperienceLevel.internship}]
    if not ordinary:
        return False, None
    evidence = entry_evidence(title, description) if any(level.needs_entry_evidence for level in ordinary) else None
    return evidence is not None or not all(level.needs_entry_evidence for level in ordinary), evidence


def match_role(
    title: str, description: str | None, titles: list[str], levels: Levels, meaning: TitleMeaning
) -> RoleMatch | None:
    """How this job fits the person's titles, at a level they chose: every word of one of the titles is in it,
    or failing that it is close to one in meaning.
    """
    fits, level = level_fit(title, description, levels)
    if not fits:
        return None
    words = _title_words(title)
    same_job = [role for role in titles if not words & (_DIFFERENT_JOB_WORDS - _core([role]))]
    for role in same_job:
        if (core := _core([role])) and core <= words:
            return RoleMatch(role, RoleKind.exact, level)
    near = meaning.closest(title, same_job)
    return RoleMatch(near, RoleKind.similar, level) if near else None


def entry_evidence(title: str, description: str | None) -> str | None:
    """Where the job shows it is entry level, or None when it doesn't (D46).

    A plain "Backend Developer" with nothing in the advert about level is left out:
    someone asking for graduate and junior roles sees only those.
    """
    if NOT_ENTRY_TITLE.search(title):
        return None
    if ENTRY_TITLE.search(title):
        return ENTRY_IN_TITLE
    text = description or ""
    if YEARS_REQUIRED.search(text):
        return None
    for found in _ENTRY_ADVERT.finditer(text):
        if not _NEGATED.search(text[max(0, found.start() - 14) : found.start()]):
            return ENTRY_IN_ADVERT
    return None


def match_by_skills(title: str, description: str | None, skills: SkillFit, levels: Levels) -> RoleMatch | None:
    """A job outside the target roles that the applicant's skills fit (D44). Level rules still apply."""
    if len(skills.matched) < SKILLS_MATCH_MIN or skills.share < SKILLS_MATCH_SHARE:
        return None
    fits, level = level_fit(title, description, levels)
    return RoleMatch("", RoleKind.skills, level) if fits else None


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
            if remote_type == "remote" or not _covers(place, want.country):
                continue
            if place["city"] is None:
                return True, f"anywhere in {place['country'] or place['region']}"
            whole_country = not want.city
            if whole_country:
                return True, f"in {place['city']}"
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
    if fit.role.level in (ENTRY_IN_TITLE, ENTRY_IN_ADVERT, ENTRY_BY_AI, _INTERNSHIP_LEVEL):
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
    if fit.role.kind == RoleKind.skills:
        missing.append("target role")
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


def showable(session: Session, now: datetime) -> list[JobFacts]:
    return [facts for facts, quality in check_all(session, now) if _can_be_shown(facts, quality)]


def match_all(session: Session, *, now: datetime, applicant_ids: set[uuid.UUID] | None = None) -> MatchReport:
    """Safe to repeat. Afterwards the table holds exactly the current matches.

    `applicant_ids` limits the run to those people, as when someone saves their profile.
    """
    report = MatchReport()
    shown = showable(session, now)
    levels = saved_levels(session)
    skills = load_skills(session, now=now)
    required = {facts.job_id: required_in_advert(facts.description) for facts in shown}
    profiles = load_profiles(session, applicant_ids)
    meaning = TitleMeaning(semantic.local_embedder)
    job_digests = {
        facts.job_id: digest(
            job_text(
                title=facts.title,
                company=facts.company_name,
                places=facts.places,
                apply_url=facts.apply_url,
                description=facts.description,
            )
        )
        for facts in shown
    }
    preferences = select(ApplicantPreference)
    if applicant_ids is not None:
        preferences = preferences.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    for preference in session.scalars(preferences).all():
        report.applicants += 1
        _match_applicant(
            session,
            preference,
            shown,
            report,
            skills,
            required,
            levels=chosen_or_inferred(levels.get(preference.applicant_id, frozenset()), preference.target_roles),
            titles=with_related(session, preference.target_roles),
            meaning=meaning,
            verdicts=_verdicts(session, profiles.get(preference.applicant_id), job_digests),
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
    levels: Levels,
    titles: list[str],
    meaning: TitleMeaning,
    verdicts: dict[uuid.UUID, JobVerdict],
) -> None:
    applicant_id = preference.applicant_id
    wanted = wanted_locations(preference.locations)
    expected = _expected_salary(preference)
    held = skills.credentials.get(applicant_id, set())
    fit_reports = current_reports(session, applicant_id)
    existing = {
        row.job_id: row
        for row in session.scalars(select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant_id))
    }
    kept: set[uuid.UUID] = set()
    for facts in shown:
        if facts.sector != preference.sector or training_title_shuts_out(facts.title, levels):
            continue
        verdict = verdicts.get(facts.job_id)
        if verdict is not None and not verdict.fits:
            continue
        # D52: once the full report is written, its score decides as well.
        fit_report = fit_reports.get(facts.job_id) if verdict is not None else None
        if fit_report is not None and fit_report.score < ADD_LINE:
            continue
        skill_fit = skills.fit(applicant_id, facts.job_id)
        role = match_role(facts.title, facts.description, titles, levels, meaning) or match_by_skills(
            facts.title, facts.description, skill_fit, levels
        )
        # D46 holds for the AI too: someone after only entry-level roles sees an AI match only when the AI
        # read the advert as entry level, not "unclear", "mid" or "senior".
        if role is None and verdict is not None and (
            ExperienceLevel.entry not in levels or verdict.level == "entry"
        ):
            role = RoleMatch(target="", kind=RoleKind.ai, level=ENTRY_BY_AI if verdict.level == "entry" else None)
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
            skills=skill_fit,
            missing_licences=[name for name in required[facts.job_id] if name not in held],
        )
        row = existing.get(facts.job_id)
        if row is None:
            row = ApplicantJobMatch(applicant_id=applicant_id, job_id=facts.job_id)
            session.add(row)
        row.score = score(fit)
        row.reasons = reasons(fit)
        if verdict is not None:
            row.reasons["ai"] = {
                "fits": True,
                "level": verdict.level,
                "reason": verdict.reason,
                "score": str(verdict.score) if verdict.score is not None else None,
                "ratings": verdict.ratings,
            }
        if fit_report is not None:
            row.reasons["report"] = fit_report.report
        row.role_match = role.kind != RoleKind.skills
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


def _verdicts(
    session: Session, profile: Profile | None, job_digests: dict[uuid.UUID, str]
) -> dict[uuid.UUID, JobVerdict]:
    return current_verdicts(session, profile, job_digests) if profile is not None else {}
