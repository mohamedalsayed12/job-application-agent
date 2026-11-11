"""The admin quality report from MVP section 16, worked out from what the database holds now."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cleaning.clean import FULL_READS
from app.cleaning.linking import is_primary
from app.models import (
    ApplicantJobMatch,
    ApplicantPreference,
    BudgetPolicy,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    JobSummary,
    SpendLedger,
)
from app.quality.checks import (
    Confidence,
    JobFacts,
    JobQuality,
    NotRecent,
    check_job,
    find_same_adverts,
)

COMPLETE_TARGET = 95.0
DUPLICATE_TARGET = 2.0
EXAMPLES = 5


@dataclass(slots=True)
class JobRow:
    label: str
    jobs: int
    live: int
    recent_pct: float
    real_high_pct: float
    complete_pct: float
    unique_pct: float
    duplicate_pct: float
    expired_pct: float
    last_success: datetime | None


@dataclass(slots=True)
class Target:
    name: str
    goal: str
    value: str
    met: bool


@dataclass(slots=True)
class Example:
    company: str
    title: str
    detail: str


@dataclass(slots=True)
class JobsSection:
    targets: list[Target]
    total: JobRow
    by_source: list[JobRow]
    by_sector: list[JobRow]
    same_advert_elsewhere: int
    older_than_60_days: int
    real_levels: dict[str, int]
    missing_counts: dict[str, int]
    not_recent_counts: dict[str, int]
    incomplete_examples: list[Example]
    duplicate_examples: list[Example]
    lower_confidence_examples: list[Example]


@dataclass(slots=True)
class SpendSection:
    total: Decimal
    this_month: Decimal
    by_kind_this_month: dict[str, Decimal]
    caps: list[tuple[str, str, Decimal | None, int | None, str]]


@dataclass(slots=True)
class CollectionSection:
    outcomes_by_source: dict[str, dict[str, int]]
    jobs_by_freshness: dict[str, int]


@dataclass(slots=True)
class RelevanceSection:
    """Per applicant, from stored matches. Kept apart from the four quality measures, so bad
    matching can be told apart from bad data (MVP section 14)."""

    applicants: int
    applicants_with_matches: int
    matches: int
    fitting_location: int
    naming_skills: int
    skill_share_pct: float | None
    missing_licences: int


@dataclass(slots=True)
class SummariesSection:
    accepted: int
    rejected: int
    in_progress: int
    cost: Decimal


@dataclass(slots=True)
class QualityReport:
    generated_at: datetime
    jobs: JobsSection
    spend: SpendSection
    collection: CollectionSection
    relevance: RelevanceSection
    summaries: SummariesSection
    gaps: list[str] = field(default_factory=list)


def load_job_facts(session: Session) -> list[JobFacts]:
    postings: dict = defaultdict(list)
    for job_id, posting in session.execute(
        select(JobLink.job_id, JobPosting).join(JobPosting, JobPosting.id == JobLink.job_posting_id)
    ).all():
        postings[job_id].append(posting)

    facts = []
    for job in session.scalars(select(Job)).all():
        linked = postings[job.id]
        primary = next((posting for posting in linked if is_primary(job, posting)), linked[0] if linked else None)
        facts.append(
            JobFacts(
                job_id=job.id,
                company_name=job.company_name,
                title=job.title,
                description=job.description,
                apply_url=job.apply_url,
                places=job.places,
                remote_type=job.remote_type,
                freshness=job.freshness,
                last_seen_at=job.last_seen_at,
                first_seen_at=job.first_seen_at,
                posted_at=primary.posted_at if primary else None,
                closes_at=primary.closes_at if primary else None,
                source=primary.source if primary else "unknown",
                sector=job.sector,
            )
        )
    return facts


def check_all(session: Session, now: datetime) -> list[tuple[JobFacts, JobQuality]]:
    """Closed jobs are checked but never counted as duplicates of the jobs still listed."""
    facts = load_job_facts(session)
    same_adverts = find_same_adverts(job for job in facts if job.freshness != JobFreshness.closed)
    return [(job, check_job(job, now, same_adverts.get(job.job_id))) for job in facts]


def store_results(session: Session, *, now: datetime) -> int:
    results = check_all(session, now)
    stored = {job.job_id: quality for job, quality in results}
    for job in session.scalars(select(Job)).all():
        job.quality_results = stored[job.id].to_json(now)
    session.flush()
    return len(stored)


def build_report(session: Session, *, now: datetime | None = None) -> QualityReport:
    now = now or datetime.now(UTC)
    return QualityReport(
        generated_at=now,
        jobs=_jobs_section(session, check_all(session, now)),
        spend=_spend_section(session, now),
        collection=_collection_section(session),
        relevance=_relevance_section(session),
        summaries=_summaries_section(session),
        gaps=[
            "Real: Companies House is not connected, so it is not one of the signals.",
            "Unique: LinkedIn is the only source, so every duplicate found is within it.",
            "Closed: LinkedIn does not say when a job closes, so a job closes when no search has found it for a while.",
            "Relevance: skills are those an advert names, which includes skills it mentions without requiring.",
        ],
    )


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _row(label: str, results: list[tuple[JobFacts, JobQuality]], last_success: datetime | None) -> JobRow:
    live = [quality for job, quality in results if job.freshness != JobFreshness.closed]
    expired = sum(
        1 for _, quality in results if quality.not_recent in (NotRecent.closed, NotRecent.past_closing_date)
    )
    duplicates = sum(1 for quality in live if not quality.unique)
    return JobRow(
        label=label,
        jobs=len(results),
        live=len(live),
        recent_pct=_pct(sum(1 for quality in live if quality.recent), len(live)),
        real_high_pct=_pct(sum(1 for quality in live if quality.confidence == Confidence.high), len(live)),
        complete_pct=_pct(sum(1 for quality in live if quality.complete), len(live)),
        unique_pct=_pct(len(live) - duplicates, len(live)),
        duplicate_pct=_pct(duplicates, len(live)),
        expired_pct=_pct(expired, len(results)),
        last_success=last_success,
    )


def _rows_by(
    results: list[tuple[JobFacts, JobQuality]], name: str, last_success: dict[str, datetime]
) -> list[JobRow]:
    groups: dict[str, list] = defaultdict(list)
    for result in results:
        groups[getattr(result[0], name)].append(result)
    return [_row(label, groups[label], last_success.get(label)) for label in sorted(groups)]


def _jobs_section(session: Session, results: list[tuple[JobFacts, JobQuality]]) -> JobsSection:
    success_by_source = dict(
        session.execute(
            select(FetchRun.source, func.max(FetchRun.finished_at))
            .where(FetchRun.outcome.in_(FULL_READS))
            .group_by(FetchRun.source)
        ).all()
    )
    live = [(job, quality) for job, quality in results if job.freshness != JobFreshness.closed]
    total = _row("All jobs", results, max(success_by_source.values(), default=None))
    expired_shown = sum(1 for job, quality in live if job.freshness == JobFreshness.open and not quality.recent)
    titles = {job.job_id: job for job, _ in results}
    return JobsSection(
        targets=[
            Target(
                "Complete",
                f"{COMPLETE_TARGET:.0f}%+",
                f"{total.complete_pct}%",
                bool(live) and total.complete_pct >= COMPLETE_TARGET,
            ),
            Target("Expired jobs shown", "0", str(expired_shown), bool(live) and expired_shown == 0),
            Target(
                "Duplicates",
                f"under {DUPLICATE_TARGET:.0f}%",
                f"{total.duplicate_pct}%",
                bool(live) and total.duplicate_pct < DUPLICATE_TARGET,
            ),
        ],
        total=total,
        by_source=_rows_by(results, "source", success_by_source),
        by_sector=_rows_by(results, "sector", {}),
        same_advert_elsewhere=sum(1 for _, q in live if q.same_advert and q.unique),
        older_than_60_days=sum(1 for _, q in live if q.older_than_60_days),
        real_levels=dict(Counter(str(q.confidence) for _, q in live)),
        missing_counts=dict(Counter(name for _, q in live for name in q.missing)),
        not_recent_counts=dict(Counter(str(q.not_recent) for _, q in results if q.not_recent)),
        incomplete_examples=[
            Example(job.company_name, job.title, ", ".join(q.missing)) for job, q in live if q.missing
        ][:EXAMPLES],
        duplicate_examples=[
            Example(
                job.company_name,
                job.title,
                f"{_place_names(job)}, same advert as {_place_names(titles[q.same_advert.of_job])}",
            )
            for job, q in live
            if not q.unique
        ][:EXAMPLES],
        lower_confidence_examples=[
            Example(job.company_name, job.title, f"{q.confidence}: {'; '.join(q.reasons)}")
            for job, q in live
            if q.confidence != Confidence.high
        ][:EXAMPLES],
    )


def _place_names(job: JobFacts) -> str:
    return ", ".join(place["city"] or place["country"] or place.get("region") or "" for place in job.places) or "no place"


def _spend_section(session: Session, now: datetime) -> SpendSection:
    cost = func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    policies = session.scalars(
        select(BudgetPolicy)
        .where(
            BudgetPolicy.effective_from <= now,
            (BudgetPolicy.effective_to.is_(None)) | (BudgetPolicy.effective_to > now),
        )
        .order_by(BudgetPolicy.provider, BudgetPolicy.period)
    ).all()
    by_kind = session.execute(
        select(SpendLedger.kind, func.sum(cost)).where(SpendLedger.occurred_at >= month_start).group_by(SpendLedger.kind)
    ).all()
    return SpendSection(
        total=Decimal(session.scalar(select(func.coalesce(func.sum(cost), 0)))),
        this_month=Decimal(
            session.scalar(select(func.coalesce(func.sum(cost), 0)).where(SpendLedger.occurred_at >= month_start))
        ),
        by_kind_this_month={kind: Decimal(amount) for kind, amount in sorted(by_kind)},
        caps=[(row.provider, row.period, row.money_cap, row.request_cap, row.currency) for row in policies],
    )


def _collection_section(session: Session) -> CollectionSection:
    outcomes: dict[str, dict[str, int]] = defaultdict(dict)
    for source, outcome, count in session.execute(
        select(FetchRun.source, FetchRun.outcome, func.count()).group_by(FetchRun.source, FetchRun.outcome)
    ).all():
        outcomes[source][outcome] = count
    return CollectionSection(
        outcomes_by_source=dict(outcomes),
        jobs_by_freshness=dict(session.execute(select(Job.freshness, func.count()).group_by(Job.freshness)).all()),
    )


def _relevance_section(session: Session) -> RelevanceSection:
    rows = session.execute(
        select(
            ApplicantJobMatch.applicant_id,
            ApplicantJobMatch.location_fit,
            func.cardinality(ApplicantJobMatch.matched_skills),
            func.cardinality(ApplicantJobMatch.missing_skills),
            func.cardinality(ApplicantJobMatch.missing_licences),
        )
    ).all()
    named = [(matched, missing) for _, _, matched, missing, _ in rows if matched + missing]
    return RelevanceSection(
        applicants=session.scalar(select(func.count()).select_from(ApplicantPreference)),
        applicants_with_matches=len({row[0] for row in rows}),
        matches=len(rows),
        fitting_location=sum(1 for row in rows if row[1]),
        naming_skills=len(named),
        skill_share_pct=_pct(sum(m for m, _ in named), sum(m + n for m, n in named)) if named else None,
        missing_licences=sum(1 for row in rows if row[4]),
    )


def _summaries_section(session: Session) -> SummariesSection:
    counts = dict(session.execute(select(JobSummary.status, func.count()).group_by(JobSummary.status)).all())
    return SummariesSection(
        accepted=counts.get("accepted", 0),
        rejected=counts.get("rejected", 0),
        in_progress=counts.get("pending", 0),
        cost=Decimal(session.scalar(select(func.coalesce(func.sum(JobSummary.cost_amount), 0)))),
    )
