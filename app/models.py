import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

TZ = DateTime(timezone=True)


class FetchOutcome(StrEnum):
    complete = "complete"
    partial = "partial"
    failed = "failed"
    not_found = "not_found"
    empty = "empty"


class EmployerType(StrEnum):
    direct = "direct"
    agency = "agency"


class JobLinkMethod(StrEnum):
    exact_job_id = "exact_job_id"
    strong_match = "strong_match"
    reviewer = "reviewer"


class JobFreshness(StrEnum):
    open = "open"
    closed = "closed"
    unknown = "unknown"


class SalaryStatus(StrEnum):
    meets = "meets"
    below = "below"
    unknown = "unknown"


class ExperienceLevel(StrEnum):
    internship = "internship"
    entry = "entry"
    mid = "mid"
    senior = "senior"


class CvFileType(StrEnum):
    pdf = "pdf"
    docx = "docx"


class SkillSource(StrEnum):
    esco = "esco"
    local = "local"


class SkillMatchMethod(StrEnum):
    exact = "exact"
    alternative_label = "alternative_label"


class SummaryStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class SpendKind(StrEnum):
    ai_summary = "ai_summary"
    ai_cv_read = "ai_cv_read"
    ai_job_judge = "ai_job_judge"
    ai_fit_report = "ai_fit_report"
    ai_cv_tailor = "ai_cv_tailor"
    linkedin_jobs = "linkedin_jobs"
    indeed_jobs = "indeed_jobs"
    # Spend recorded under kinds that no longer exist. Kept so the monthly cap still counts it.
    retired = "retired"


class TailoredCvStatus(StrEnum):
    pending = "pending"
    waiting_budget = "waiting_budget"
    ready = "ready"
    skipped = "skipped"
    failed = "failed"


class CompanyResearchStatus(StrEnum):
    ready = "ready"
    unavailable = "unavailable"




def _text_in(
    column: str,
    allowed: type[StrEnum],
    *,
    nullable: bool = False,
) -> CheckConstraint:
    literals = ", ".join(f"'{member.value}'" for member in allowed)
    predicate = f"{column} IN ({literals})"
    if nullable:
        predicate = f"{column} IS NULL OR {predicate}"
    return CheckConstraint(predicate, name=column)


class _UuidPk:
    # sort_order puts the mixin column before the class body. Default 0
    # places subclass columns first on SQLAlchemy 2.0.4+.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        sort_order=-1,
    )


def _created():
    return mapped_column(TZ, server_default=func.now())


def _updated():
    return mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Applicant(_UuidPk, Base):
    __tablename__ = "applicant"

    email: Mapped[str] = mapped_column(CITEXT(), unique=True)
    display_name: Mapped[str | None] = mapped_column(Text())
    is_active: Mapped[bool] = mapped_column(Boolean(), server_default=text("true"))
    # Null means no one can log in yet: an admin-created applicant, or a test profile.
    password_hash: Mapped[str | None] = mapped_column(Text())
    # Stored in the login cookie. Raising it ends every session for this applicant.
    session_version: Mapped[int] = mapped_column(Integer(), default=0, server_default=text("0"))
    created_at: Mapped[datetime] = _created()


class BudgetPolicy(_UuidPk, Base):
    __tablename__ = "budget_policy"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "period",
            "effective_from",
            name="uq_budget_policy_provider_period_effective_from",
        ),
    )

    provider: Mapped[str] = mapped_column(Text())
    period: Mapped[str] = mapped_column(Text())
    money_cap: Mapped[Decimal | None] = mapped_column(Numeric())
    request_cap: Mapped[int | None] = mapped_column(Integer())
    currency: Mapped[str] = mapped_column(Text())
    effective_from: Mapped[datetime] = mapped_column(TZ)
    effective_to: Mapped[datetime | None] = mapped_column(TZ)


class ApplicantPreference(_UuidPk, Base):
    __tablename__ = "applicant_preference"
    __table_args__ = (
        CheckConstraint("cardinality(target_roles) >= 1", name="target_roles_nonempty"),
        CheckConstraint("cardinality(locations) >= 1", name="locations_nonempty"),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("applicant.id", ondelete="CASCADE"),
        unique=True,
    )
    target_roles: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    locations: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    sector: Mapped[str] = mapped_column(Text(), server_default=text("'tech'"))
    salary_amount: Mapped[Decimal | None] = mapped_column(Numeric())
    salary_currency: Mapped[str | None] = mapped_column(Text())
    salary_period: Mapped[str | None] = mapped_column(Text())
    updated_at: Mapped[datetime] = _updated()


class Skill(_UuidPk, Base):
    """One taxonomy entry. `local` entries are ours, added where ESCO has no entry (D39)."""

    __tablename__ = "skill"
    __table_args__ = (
        UniqueConstraint("taxonomy_id", name="uq_skill_taxonomy_id"),
        _text_in("source", SkillSource),
    )

    taxonomy_id: Mapped[str] = mapped_column(Text())
    source: Mapped[str] = mapped_column(Text())
    preferred_label: Mapped[str] = mapped_column(Text())
    alternative_labels: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), default=list, server_default=text("'{}'")
    )
    # Only technical skills are looked for in job adverts. Everyday labels would match ordinary words.
    is_technical: Mapped[bool] = mapped_column(Boolean(), server_default=text("false"))
    taxonomy_version: Mapped[str] = mapped_column(Text())
    updated_at: Mapped[datetime] = _updated()


class SkillLabel(_UuidPk, Base):
    """Every name a skill goes by, in one searchable form."""

    __tablename__ = "skill_label"
    __table_args__ = (
        UniqueConstraint("skill_id", "normalized", name="uq_skill_label_skill_id_normalized"),
        Index("ix_skill_label_normalized", "normalized", postgresql_ops={"normalized": "text_pattern_ops"}),
    )

    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("skill.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(Text())
    normalized: Mapped[str] = mapped_column(Text())
    is_preferred: Mapped[bool] = mapped_column(Boolean())


class JobSkill(_UuidPk, Base):
    __tablename__ = "job_skill"
    __table_args__ = (
        UniqueConstraint("job_id", "skill_id", name="uq_job_skill_job_id_skill_id"),
        _text_in("how_matched", SkillMatchMethod),
        Index("ix_job_skill_skill_id", "skill_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"))
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("skill.id", ondelete="CASCADE"))
    matched_text: Mapped[str] = mapped_column(Text())
    how_matched: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


class ApplicantProfile(_UuidPk, Base):
    __tablename__ = "applicant_profile"
    __table_args__ = (
        _text_in("experience_level", ExperienceLevel),
        CheckConstraint(
            "travel_distance_miles IS NULL OR travel_distance_miles BETWEEN 1 AND 500",
            name="travel_distance_miles",
        ),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("applicant.id", ondelete="CASCADE"),
        unique=True,
    )
    experience_level: Mapped[str] = mapped_column(Text())
    travel_distance_miles: Mapped[int | None] = mapped_column(Integer())
    # As typed, such as "BSc Business Management". Studying for one counts (D45).
    degrees: Mapped[list[str]] = mapped_column(ARRAY(Text()), default=list, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = _updated()


class ApplicantSkill(_UuidPk, Base):
    __tablename__ = "applicant_skill"
    __table_args__ = (
        UniqueConstraint(
            "applicant_id",
            "text_as_typed",
            name="uq_applicant_skill_applicant_id_text_as_typed",
        ),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="CASCADE"))
    text_as_typed: Mapped[str] = mapped_column(CITEXT())
    # Null when what they typed is not in the taxonomy. It stays saved as typed.
    skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("skill.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class ApplicantCertification(_UuidPk, Base):
    __tablename__ = "applicant_certification"

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("applicant.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text())
    issuer: Mapped[str | None] = mapped_column(Text())
    is_licence: Mapped[bool] = mapped_column(Boolean(), server_default=text("false"))
    expires_on: Mapped[date | None] = mapped_column(Date())
    created_at: Mapped[datetime] = _created()


class CvFile(_UuidPk, Base):
    """D38: the file itself lives here, loaded only when its owner downloads it."""

    __tablename__ = "cv_file"
    __table_args__ = (
        _text_in("file_type", CvFileType),
        CheckConstraint("size_bytes BETWEEN 1 AND 5242880", name="size_bytes"),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("applicant.id", ondelete="CASCADE"),
        unique=True,
    )
    content: Mapped[bytes] = mapped_column(LargeBinary(), deferred=True)
    file_type: Mapped[str] = mapped_column(Text())
    size_bytes: Mapped[int] = mapped_column(Integer())
    uploaded_at: Mapped[datetime] = mapped_column(TZ)


class SpendLedger(_UuidPk, Base):
    __tablename__ = "spend_ledger"
    __table_args__ = (
        _text_in("kind", SpendKind),
        Index("ix_spend_ledger_provider_occurred_at", "provider", "occurred_at"),
    )

    provider: Mapped[str] = mapped_column(Text())
    kind: Mapped[str] = mapped_column(Text())
    related_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric())
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric())
    units: Mapped[Decimal] = mapped_column(Numeric())
    unit_label: Mapped[str] = mapped_column(Text())
    occurred_at: Mapped[datetime] = mapped_column(TZ)


class FetchRun(_UuidPk, Base):
    __tablename__ = "fetch_run"
    __table_args__ = (_text_in("outcome", FetchOutcome),)

    source: Mapped[str] = mapped_column(Text())
    outcome: Mapped[str] = mapped_column(Text())
    http_status: Mapped[int | None] = mapped_column(Integer())
    jobs_returned: Mapped[int] = mapped_column(Integer())
    request_count: Mapped[int] = mapped_column(Integer())
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime] = mapped_column(TZ)


class RawPayload(_UuidPk, Base):
    __tablename__ = "raw_payload"
    __table_args__ = (
        Index(
            "ix_raw_payload_source_job_fetched",
            "source",
            "source_job_id",
            desc("fetched_at"),
        ),
    )

    fetch_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("fetch_run.id"))
    source: Mapped[str] = mapped_column(Text())
    source_job_id: Mapped[str | None] = mapped_column(Text())
    payload: Mapped[Any] = mapped_column(JSONB())
    payload_hash: Mapped[str] = mapped_column(Text())
    fetched_at: Mapped[datetime] = mapped_column(TZ)


class JobPosting(_UuidPk, Base):
    __tablename__ = "job_posting"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_job_id",
            name="uq_job_posting_source_source_job_id",
        ),
        _text_in("employer_type", EmployerType),
    )

    source: Mapped[str] = mapped_column(Text())
    source_job_id: Mapped[str] = mapped_column(Text())
    raw_payload_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("raw_payload.id"),
    )
    title: Mapped[str] = mapped_column(Text())
    company_name: Mapped[str] = mapped_column(Text())
    employer_type: Mapped[str] = mapped_column(Text())
    location_city: Mapped[str | None] = mapped_column(Text())
    location_country: Mapped[str | None] = mapped_column(Text())
    remote_type: Mapped[str | None] = mapped_column(Text())
    places: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), default=list, server_default=text("'[]'::jsonb")
    )
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric())
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric())
    salary_currency: Mapped[str | None] = mapped_column(Text())
    salary_period: Mapped[str | None] = mapped_column(Text())
    salary_known: Mapped[bool] = mapped_column(Boolean())
    hours: Mapped[str | None] = mapped_column(Text())
    contract_type: Mapped[str | None] = mapped_column(Text())
    description: Mapped[str | None] = mapped_column(Text())
    apply_url: Mapped[str] = mapped_column(Text())
    posted_at: Mapped[datetime | None] = mapped_column(TZ)
    closes_at: Mapped[datetime | None] = mapped_column(TZ)
    first_seen_at: Mapped[datetime] = mapped_column(TZ)
    last_seen_at: Mapped[datetime] = mapped_column(TZ)
    last_seen_complete_fetch_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Job(_UuidPk, Base):
    __tablename__ = "job"
    __table_args__ = (
        _text_in("freshness", JobFreshness),
        Index("ix_job_freshness", "freshness"),
    )

    title: Mapped[str] = mapped_column(Text())
    company_name: Mapped[str] = mapped_column(Text())
    location_city: Mapped[str | None] = mapped_column(Text())
    location_country: Mapped[str | None] = mapped_column(Text())
    remote_type: Mapped[str | None] = mapped_column(Text())
    places: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(), default=list, server_default=text("'[]'::jsonb")
    )
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric())
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric())
    salary_currency: Mapped[str | None] = mapped_column(Text())
    salary_period: Mapped[str | None] = mapped_column(Text())
    salary_known: Mapped[bool] = mapped_column(Boolean())
    description: Mapped[str | None] = mapped_column(Text())
    apply_url: Mapped[str] = mapped_column(Text())
    sector: Mapped[str] = mapped_column(Text())
    freshness: Mapped[str] = mapped_column(Text())
    field_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB())
    closure_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    quality_results: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    first_seen_at: Mapped[datetime] = mapped_column(TZ)
    last_seen_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class JobLink(_UuidPk, Base):
    __tablename__ = "job_link"
    __table_args__ = (_text_in("method", JobLinkMethod),)

    job_posting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("job_posting.id"),
        unique=True,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id"))
    method: Mapped[str] = mapped_column(Text())
    confidence: Mapped[Decimal] = mapped_column(Numeric())
    reason: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


class ApplicantJobMatch(_UuidPk, Base):
    __tablename__ = "applicant_job_match"
    __table_args__ = (
        UniqueConstraint(
            "applicant_id",
            "job_id",
            name="uq_applicant_job_match_applicant_id_job_id",
        ),
        _text_in("salary_status", SalaryStatus),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id"))
    score: Mapped[Decimal] = mapped_column(Numeric())
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB())
    role_match: Mapped[bool] = mapped_column(Boolean())
    location_fit: Mapped[bool] = mapped_column(Boolean())
    salary_status: Mapped[str] = mapped_column(Text())
    # D40: skill names as shown, and certification or licence groups the advert requires.
    matched_skills: Mapped[list[str]] = mapped_column(ARRAY(Text()), default=list, server_default=text("'{}'"))
    missing_skills: Mapped[list[str]] = mapped_column(ARRAY(Text()), default=list, server_default=text("'{}'"))
    missing_licences: Mapped[list[str]] = mapped_column(ARRAY(Text()), default=list, server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class JobSummary(_UuidPk, Base):
    """D41: one per job, written on first view. Only an accepted summary is ever shown."""

    __tablename__ = "job_summary"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_job_summary_job_id"),
        _text_in("status", SummaryStatus),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text())
    # The draft, kept when rejected so an admin can see why.
    summary_text: Mapped[str | None] = mapped_column(Text())
    unsupported: Mapped[list[str]] = mapped_column(ARRAY(Text()), default=list, server_default=text("'{}'"))
    # The posting the summary was written from. A changed posting gets a new summary.
    source_hash: Mapped[str] = mapped_column(Text())
    model: Mapped[str] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    prompt_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(), server_default=text("0"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class JobEmbedding(Base):
    """D48: a job's meaning as a vector, for the search box on the listings page."""

    __tablename__ = "job_embedding"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(Text())
    # The text the vector was made from. A new summary or description gets a new vector.
    source_hash: Mapped[str] = mapped_column(Text())
    vector: Mapped[list[float]] = mapped_column(ARRAY(Float()))
    updated_at: Mapped[datetime] = _updated()


class JobVerdict(_UuidPk, Base):
    """D50: the AI's judgement of whether one job fits one person, kept until the job or profile changes."""

    __tablename__ = "job_verdict"
    __table_args__ = (UniqueConstraint("applicant_id", "job_id", name="uq_job_verdict_applicant_job"),)

    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"))
    fits: Mapped[bool] = mapped_column(Boolean())
    # entry, mid, senior or unclear, as the AI read the advert.
    level: Mapped[str] = mapped_column(Text())
    # Shown to the person only when every name and number in it is in the advert or their profile.
    reason: Mapped[str | None] = mapped_column(Text())
    # The overall 1-5 score; fits means 4.0 or more. Null on verdicts made before the score existed.
    score: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    # The 1-5 rating per area (cv_match, role_alignment, comp, culture) and the red flags it named.
    ratings: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    job_hash: Mapped[str] = mapped_column(Text())
    profile_hash: Mapped[str] = mapped_column(Text())
    model: Mapped[str] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    prompt_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(), server_default=text("0"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()



class JobReport(_UuidPk, Base):
    """D52: the AI's full fit report for one job and one person, written for jobs scoring 3.0 or more."""

    __tablename__ = "job_report"
    __table_args__ = (UniqueConstraint("applicant_id", "job_id", name="uq_job_report_applicant_job"),)

    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"))
    # Apply, Consider, Research first or Skip.
    verdict: Mapped[str] = mapped_column(Text())
    score: Mapped[Decimal] = mapped_column(Numeric(2, 1))
    # Verdict sentence, requirements table, five criteria, blockers, confidence and questions, checked by code.
    report: Mapped[dict[str, Any]] = mapped_column(JSONB())
    # Hash of the job text and everything sent about the person, CV included; a change means a new report.
    input_hash: Mapped[str] = mapped_column(Text())
    model: Mapped[str] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    prompt_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(), server_default=text("0"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class CompanyResearch(_UuidPk, Base):
    """Cached public mission / vision / values for one employer name (D55)."""

    __tablename__ = "company_research"
    __table_args__ = (
        UniqueConstraint("company_key", name="uq_company_research_company_key"),
        _text_in("status", CompanyResearchStatus),
    )

    company_key: Mapped[str] = mapped_column(Text())
    company_name: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(Text())
    # Mission, vision, goals, values, evidence quotes and source URLs when status is ready.
    research: Mapped[dict[str, Any]] = mapped_column(JSONB())
    official_domain: Mapped[str | None] = mapped_column(Text())
    fetched_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class TailoredCv(_UuidPk, Base):
    """D55: one tailored CV package per person and Apply/Consider job."""

    __tablename__ = "tailored_cv"
    __table_args__ = (
        UniqueConstraint("applicant_id", "job_id", name="uq_tailored_cv_applicant_job"),
        _text_in("status", TailoredCvStatus),
        CheckConstraint(
            "pdf_size_bytes IS NULL OR (pdf_size_bytes BETWEEN 1 AND 5242880)",
            name="pdf_size_bytes",
        ),
        CheckConstraint(
            "docx_size_bytes IS NULL OR (docx_size_bytes BETWEEN 1 AND 5242880)",
            name="docx_size_bytes",
        ),
    )

    applicant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("job.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text())
    # Hash of advert, CV bytes, profile and company-research input; a change means a new package.
    input_hash: Mapped[str] = mapped_column(Text())
    # Structured draft after grounding, keywords list, ATS coverage table.
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    keywords: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    ats_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    company_research_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("company_research.id", ondelete="SET NULL")
    )
    company_research_available: Mapped[bool] = mapped_column(Boolean(), server_default=text("false"))
    pdf_content: Mapped[bytes | None] = mapped_column(LargeBinary(), deferred=True)
    docx_content: Mapped[bytes | None] = mapped_column(LargeBinary(), deferred=True)
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer())
    docx_size_bytes: Mapped[int | None] = mapped_column(Integer())
    model: Mapped[str | None] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    prompt_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(), server_default=text("0"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()