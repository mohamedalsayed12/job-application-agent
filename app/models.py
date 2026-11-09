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
from sqlalchemy.sql.elements import TextClause

from app.core.db import Base

TZ = DateTime(timezone=True)


class SearchTemplateStatus(StrEnum):
    unproven = "unproven"
    active = "active"
    retired = "retired"


class SearchRunStatus(StrEnum):
    planned = "planned"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    inconclusive = "inconclusive"


class CandidateState(StrEnum):
    new = "new"
    duplicate = "duplicate"
    queued = "queued"
    verifying = "verifying"
    retry_wait = "retry_wait"
    needs_review = "needs_review"
    verified = "verified"
    registered = "registered"
    unsupported = "unsupported"
    rejected = "rejected"


class ExtractedBy(StrEnum):
    code = "code"
    ai = "ai"


class VerificationOutcome(StrEnum):
    pass_ = "pass"
    fail = "fail"
    inconclusive = "inconclusive"
    error = "error"


class ReviewDecision(StrEnum):
    verified = "verified"
    rejected = "rejected"
    unsupported = "unsupported"


class EvidenceLevel(StrEnum):
    strong = "strong"
    medium = "medium"
    weak = "weak"


class BoardStatus(StrEnum):
    verified = "verified"
    needs_reverify = "needs_reverify"
    retired = "retired"


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
    search = "search"
    ai_extraction = "ai_extraction"
    ai_summary = "ai_summary"


CANDIDATE_CLAIMABLE = (CandidateState.queued, CandidateState.retry_wait)
SEARCH_RUN_CLAIMABLE = (SearchRunStatus.planned, SearchRunStatus.failed)
CANDIDATE_LIVE_UNIQUE_EXCLUDES = (CandidateState.duplicate, CandidateState.rejected)


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


def _in(column: str, members: tuple[StrEnum, ...]) -> TextClause:
    literals = ", ".join(f"'{member.value}'" for member in members)
    return text(f"{column} IN ({literals})")


def _excluded_states() -> str:
    return ", ".join(f"'{member.value}'" for member in CANDIDATE_LIVE_UNIQUE_EXCLUDES)


def _live_candidate_board_where() -> TextClause:
    return text(
        "board_system IS NOT NULL AND board_instance IS NOT NULL "
        "AND board_id IS NOT NULL "
        f"AND state NOT IN ({_excluded_states()})"
    )


def _live_candidate_domain_where() -> TextClause:
    return text(
        f"primary_domain IS NOT NULL AND state NOT IN ({_excluded_states()})"
    )


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


class SearchTemplate(Base):
    __tablename__ = "search_template"
    __table_args__ = (_text_in("status", SearchTemplateStatus),)

    id: Mapped[str] = mapped_column(Text(), primary_key=True)
    search_type: Mapped[str] = mapped_column(Text())
    provider: Mapped[str] = mapped_column(Text())
    pattern: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(Text())
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


class AiExtraction(_UuidPk, Base):
    __tablename__ = "ai_extraction"
    __table_args__ = (
        UniqueConstraint(
            "input_hash",
            "prompt_version",
            name="uq_ai_extraction_input_hash_prompt_version",
        ),
    )

    input_hash: Mapped[str] = mapped_column(Text())
    model: Mapped[str] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    output: Mapped[dict[str, Any]] = mapped_column(JSONB())
    grounded_count: Mapped[int] = mapped_column(Integer())
    ungrounded_count: Mapped[int] = mapped_column(Integer())
    cost_amount: Mapped[Decimal] = mapped_column(Numeric())
    created_at: Mapped[datetime] = _created()


class Employer(_UuidPk, Base):
    __tablename__ = "employer"
    __table_args__ = (Index("ix_employer_cleaned_name", "cleaned_name"),)

    name: Mapped[str] = mapped_column(Text())
    cleaned_name: Mapped[str] = mapped_column(Text())
    official_domain: Mapped[str] = mapped_column(Text(), unique=True)
    sector: Mapped[str] = mapped_column(Text(), server_default=text("'tech'"))
    careers_system: Mapped[str] = mapped_column(Text())
    revisit_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


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


class SearchRun(_UuidPk, Base):
    __tablename__ = "search_run"
    __table_args__ = (
        _text_in("status", SearchRunStatus),
        Index(
            "ix_search_run_claim",
            "status",
            "next_attempt_at",
            postgresql_where=_in("status", SEARCH_RUN_CLAIMABLE),
        ),
    )

    template_id: Mapped[str] = mapped_column(Text(), ForeignKey("search_template.id"))
    # Cleared when the account is deleted, so the search record and its spend stay.
    applicant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("applicant.id", ondelete="SET NULL"))
    query: Mapped[str] = mapped_column(Text())
    provider: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(Text())
    attempt_count: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZ)
    claimed_at: Mapped[datetime | None] = mapped_column(TZ)
    result_count: Mapped[int | None] = mapped_column(Integer())
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric())
    last_error: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    # Null until measured. A board or job shared between runs is split, so these can be fractions.
    new_candidates: Mapped[Decimal | None] = mapped_column(Numeric())
    new_verified_boards: Mapped[Decimal | None] = mapped_column(Numeric())
    relevant_jobs: Mapped[Decimal | None] = mapped_column(Numeric())
    freshness_share: Mapped[Decimal | None] = mapped_column(Numeric())
    outcomes_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()


class SearchResult(_UuidPk, Base):
    __tablename__ = "search_result"
    __table_args__ = (
        UniqueConstraint(
            "search_run_id",
            "rank",
            name="uq_search_result_search_run_id_rank",
        ),
        Index("ix_search_result_url", "url"),
    )

    search_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("search_run.id", ondelete="CASCADE"),
    )
    rank: Mapped[int] = mapped_column(Integer())
    url: Mapped[str] = mapped_column(Text())
    title: Mapped[str] = mapped_column(Text())
    snippet: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


class TemplateStats(Base):
    __tablename__ = "template_stats"
    __table_args__ = (
        CheckConstraint(
            "freshness_share >= 0 AND freshness_share <= 1",
            name="freshness_share_range",
        ),
    )

    template_id: Mapped[str] = mapped_column(
        Text(),
        ForeignKey("search_template.id"),
        primary_key=True,
    )
    run_count: Mapped[int] = mapped_column(Integer())
    total_cost: Mapped[Decimal] = mapped_column(Numeric())
    new_candidates: Mapped[Decimal] = mapped_column(Numeric())
    new_verified_boards: Mapped[Decimal] = mapped_column(Numeric())
    relevant_jobs: Mapped[Decimal] = mapped_column(Numeric())
    freshness_share: Mapped[Decimal] = mapped_column(Numeric())
    updated_at: Mapped[datetime] = _updated()


class SpendLedger(_UuidPk, Base):
    __tablename__ = "spend_ledger"
    __table_args__ = (
        _text_in("kind", SpendKind),
        Index("ix_spend_ledger_provider_occurred_at", "provider", "occurred_at"),
    )

    provider: Mapped[str] = mapped_column(Text())
    kind: Mapped[str] = mapped_column(Text())
    related_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    related_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric())
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric())
    units: Mapped[Decimal] = mapped_column(Numeric())
    unit_label: Mapped[str] = mapped_column(Text())
    occurred_at: Mapped[datetime] = mapped_column(TZ)


class Candidate(_UuidPk, Base):
    __tablename__ = "candidate"
    __table_args__ = (
        _text_in("state", CandidateState),
        _text_in("extracted_by", ExtractedBy),
        Index(
            "uq_candidate_live_board",
            "board_system",
            "board_instance",
            "board_id",
            unique=True,
            postgresql_where=_live_candidate_board_where(),
        ),
        Index(
            "uq_candidate_live_primary_domain",
            "primary_domain",
            unique=True,
            postgresql_where=_live_candidate_domain_where(),
        ),
        Index("ix_candidate_cleaned_name", "cleaned_name"),
        Index(
            "ix_candidate_claim",
            "state",
            "next_attempt_at",
            postgresql_where=_in("state", CANDIDATE_CLAIMABLE),
        ),
    )

    company_name: Mapped[str] = mapped_column(Text())
    cleaned_name: Mapped[str] = mapped_column(Text())
    domain_candidates: Mapped[list[str]] = mapped_column(
        ARRAY(Text()),
        server_default=text("'{}'::text[]"),
    )
    primary_domain: Mapped[str | None] = mapped_column(Text())
    board_system: Mapped[str | None] = mapped_column(Text())
    board_instance: Mapped[str | None] = mapped_column(Text())
    board_id: Mapped[str | None] = mapped_column(Text())
    state: Mapped[str] = mapped_column(Text())
    attempt_count: Mapped[int] = mapped_column(Integer(), server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZ)
    claimed_at: Mapped[datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(Text())
    extracted_by: Mapped[str] = mapped_column(Text())
    ai_extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("ai_extraction.id"),
    )
    merged_into_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("candidate.id"),
    )
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer.id"),
    )
    revisit_at: Mapped[datetime | None] = mapped_column(TZ)
    # Step 10 R2: set once a CF3 official-domain search has run for this row.
    domain_searched_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class EmployerBoard(_UuidPk, Base):
    __tablename__ = "employer_board"
    __table_args__ = (
        UniqueConstraint(
            "system",
            "instance",
            "board_id",
            name="uq_employer_board_system_instance_board_id",
        ),
        _text_in("evidence_level", EvidenceLevel),
        _text_in("status", BoardStatus),
        Index("ix_employer_board_reverify", "status", "next_reverify_at"),
    )

    employer_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("employer.id"))
    system: Mapped[str] = mapped_column(Text())
    instance: Mapped[str] = mapped_column(Text())
    board_id: Mapped[str] = mapped_column(Text())
    evidence_level: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(Text())
    verified_at: Mapped[datetime | None] = mapped_column(TZ)
    last_checked_at: Mapped[datetime | None] = mapped_column(TZ)
    next_reverify_at: Mapped[datetime | None] = mapped_column(TZ)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer(),
        server_default=text("0"),
    )
    last_job_count: Mapped[int | None] = mapped_column(Integer())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class CandidateEvidence(_UuidPk, Base):
    __tablename__ = "candidate_evidence"
    __table_args__ = (
        CheckConstraint("char_length(quoted_text) <= 200", name="quoted_text_len"),
        Index("ix_candidate_evidence_candidate_id", "candidate_id"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("candidate.id"))
    search_result_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("search_result.id"),
    )
    quoted_text: Mapped[str] = mapped_column(Text())
    source_url: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


class VerificationCheck(_UuidPk, Base):
    __tablename__ = "verification_check"
    __table_args__ = (
        _text_in("outcome", VerificationOutcome),
        CheckConstraint(
            "candidate_id IS NOT NULL OR employer_board_id IS NOT NULL",
            name="has_subject",
        ),
    )

    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("candidate.id"),
    )
    employer_board_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer_board.id"),
    )
    check_type: Mapped[str] = mapped_column(Text())
    outcome: Mapped[str] = mapped_column(Text())
    evidence_url: Mapped[str | None] = mapped_column(Text())
    evidence_snippet: Mapped[str | None] = mapped_column(Text())
    http_status: Mapped[int | None] = mapped_column(Integer())
    checked_at: Mapped[datetime] = mapped_column(TZ)


class ReviewItem(_UuidPk, Base):
    __tablename__ = "review_item"
    __table_args__ = (
        _text_in("decision", ReviewDecision, nullable=True),
        Index(
            "uq_review_item_one_open",
            "candidate_id",
            unique=True,
            postgresql_where=text("decided_at IS NULL"),
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("candidate.id"))
    reason: Mapped[str] = mapped_column(Text())
    priority: Mapped[int] = mapped_column(Integer())
    decision: Mapped[str | None] = mapped_column(Text())
    reviewer: Mapped[str | None] = mapped_column(Text())
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    notes: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


class FetchRun(_UuidPk, Base):
    __tablename__ = "fetch_run"
    __table_args__ = (_text_in("outcome", FetchOutcome),)

    source: Mapped[str] = mapped_column(Text())
    employer_board_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer_board.id"),
    )
    search_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("search_run.id"),
    )
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
        Index("ix_job_posting_employer_board_id", "employer_board_id"),
    )

    source: Mapped[str] = mapped_column(Text())
    source_job_id: Mapped[str] = mapped_column(Text())
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer.id"),
    )
    employer_board_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer_board.id"),
    )
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

    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer.id"),
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


class AuditSample(_UuidPk, Base):
    __tablename__ = "audit_sample"

    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("candidate.id"),
    )
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("employer.id"),
    )
    system_result: Mapped[str] = mapped_column(Text())
    auditor_result: Mapped[str | None] = mapped_column(Text())
    is_correct: Mapped[bool | None] = mapped_column(Boolean())
    is_useful: Mapped[bool | None] = mapped_column(Boolean())
    notes: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = _created()


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
