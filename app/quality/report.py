"""The admin quality report from MVP section 16, worked out from what the database holds now."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cleaning.freshness import BRAKE_MIN_JOBS, FULL_READS
from app.cleaning.linking import is_primary
from app.models import (
    AiExtraction,
    AuditSample,
    BoardStatus,
    BudgetPolicy,
    Candidate,
    Employer,
    EmployerBoard,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    RawPayload,
    ReviewItem,
    SearchRun,
    SpendLedger,
)
from app.quality.checks import (
    Confidence,
    JobFacts,
    JobQuality,
    NotRecent,
    check_job,
    find_duplicates,
)

COMPLETE_TARGET = 95.0
DUPLICATE_TARGET = 2.0
REPEATED_FAILURES = 2
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
    same_city_duplicates: int
    older_than_60_days: int
    real_levels: dict[str, int]
    missing_counts: dict[str, int]
    not_recent_counts: dict[str, int]
    incomplete_examples: list[Example]
    duplicate_examples: list[Example]
    lower_confidence_examples: list[Example]


@dataclass(slots=True)
class DiscoverySection:
    searches_by_template: list[tuple[str, int, Decimal]]
    candidates: int
    candidates_by_state: dict[str, int]
    review_backlog: int
    oldest_review_days: int | None
    ungrounded_pct: float | None
    boards: int
    spend_total: Decimal
    boards_per_pound: float | None
    spend_this_month: Decimal
    caps: list[tuple[str, str, Decimal | None, int | None, str]]


@dataclass(slots=True)
class CollectionSection:
    outcomes_by_source: dict[str, dict[str, int]]
    repeated_failures: list[tuple[str, int]]
    brake_events: list[tuple[str, datetime, int, int]]
    jobs_by_freshness: dict[str, int]


@dataclass(slots=True)
class AuditSection:
    sampled: int
    latest_day: str | None
    judged: int
    correct: int
    useful_judged: int
    useful: int


@dataclass(slots=True)
class QualityReport:
    generated_at: datetime
    jobs: JobsSection
    discovery: DiscoverySection
    collection: CollectionSection
    audit: AuditSection
    gaps: list[str] = field(default_factory=list)


def load_job_facts(session: Session) -> list[JobFacts]:
    domains = dict(session.execute(select(Employer.id, Employer.official_domain)).all())
    board_status = dict(session.execute(select(EmployerBoard.id, EmployerBoard.status)).all())
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
                employer_id=job.employer_id,
                company_name=job.company_name,
                title=job.title,
                description=job.description,
                apply_url=job.apply_url,
                location_city=job.location_city,
                location_country=job.location_country,
                remote_type=job.remote_type,
                freshness=job.freshness,
                last_seen_at=job.last_seen_at,
                first_seen_at=job.first_seen_at,
                posted_at=primary.posted_at if primary else None,
                closes_at=primary.closes_at if primary else None,
                board_status=board_status.get(primary.employer_board_id) if primary else None,
                official_domain=domains.get(job.employer_id),
                source=primary.source if primary else "unknown",
                sector=job.sector,
            )
        )
    return facts


def check_all(session: Session, now: datetime) -> list[tuple[JobFacts, JobQuality]]:
    """Closed jobs are checked but never counted as duplicates of the jobs still listed."""
    facts = load_job_facts(session)
    duplicates = find_duplicates(job for job in facts if job.freshness != JobFreshness.closed)
    return [(job, check_job(job, now, duplicates.get(job.job_id))) for job in facts]


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
        discovery=_discovery_section(session, now),
        collection=_collection_section(session),
        audit=_audit_section(session),
        gaps=[
            "Summaries: not built yet, so nothing has been generated, rejected or charged.",
            "Real: Companies House is not connected, so it is not one of the signals.",
            "Unique: no employer is on two sources yet, so every duplicate found is within one source.",
            "Expired jobs shown: job search does not read stored jobs yet, so this counts jobs it would show.",
            "Safety-brake events are worked out from stored fetches. Nothing records them when they happen.",
            "Duplicate candidates removed: they are folded in when found and not counted anywhere.",
            "Verified boards per £: the current boards came from the seed list, so this flatters search.",
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
    success_by_sector = dict(
        session.execute(
            select(Employer.sector, func.max(FetchRun.finished_at))
            .join(EmployerBoard, EmployerBoard.employer_id == Employer.id)
            .join(FetchRun, FetchRun.employer_board_id == EmployerBoard.id)
            .where(FetchRun.outcome.in_(FULL_READS))
            .group_by(Employer.sector)
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
        by_sector=_rows_by(results, "sector", success_by_sector),
        same_city_duplicates=sum(1 for _, q in live if q.duplicate and q.duplicate.same_city),
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
                f"{job.location_city or 'no city'}, same advert as {titles[q.duplicate.of_job].location_city or 'no city'}",
            )
            for job, q in live
            if q.duplicate
        ][:EXAMPLES],
        lower_confidence_examples=[
            Example(job.company_name, job.title, f"{q.confidence}: {'; '.join(q.reasons)}")
            for job, q in live
            if q.confidence != Confidence.high
        ][:EXAMPLES],
    )


def _discovery_section(session: Session, now: datetime) -> DiscoverySection:
    grounded, ungrounded = session.execute(
        select(
            func.coalesce(func.sum(AiExtraction.grounded_count), 0),
            func.coalesce(func.sum(AiExtraction.ungrounded_count), 0),
        )
    ).one()
    oldest_review = session.scalar(select(func.min(ReviewItem.created_at)).where(ReviewItem.decided_at.is_(None)))
    cost = func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)
    spend_total = Decimal(session.scalar(select(func.coalesce(func.sum(cost), 0))))
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    boards = session.scalar(
        select(func.count()).select_from(EmployerBoard).where(EmployerBoard.status != BoardStatus.retired)
    )
    policies = session.scalars(
        select(BudgetPolicy)
        .where(
            BudgetPolicy.effective_from <= now,
            (BudgetPolicy.effective_to.is_(None)) | (BudgetPolicy.effective_to > now),
        )
        .order_by(BudgetPolicy.provider, BudgetPolicy.period)
    ).all()
    spent_by_template = dict(
        session.execute(
            select(SearchRun.template_id, func.sum(cost))
            .join(SpendLedger, SpendLedger.related_run_id == SearchRun.id)
            .group_by(SearchRun.template_id)
        ).all()
    )
    return DiscoverySection(
        searches_by_template=[
            (template, runs, Decimal(spent_by_template.get(template, 0)))
            for template, runs in session.execute(
                select(SearchRun.template_id, func.count())
                .group_by(SearchRun.template_id)
                .order_by(SearchRun.template_id)
            ).all()
        ],
        candidates=session.scalar(select(func.count()).select_from(Candidate)),
        candidates_by_state=dict(session.execute(select(Candidate.state, func.count()).group_by(Candidate.state)).all()),
        review_backlog=session.scalar(
            select(func.count()).select_from(ReviewItem).where(ReviewItem.decided_at.is_(None))
        ),
        oldest_review_days=(now - oldest_review).days if oldest_review else None,
        ungrounded_pct=_pct(ungrounded, grounded + ungrounded) if grounded + ungrounded else None,
        boards=boards,
        spend_total=spend_total,
        boards_per_pound=round(boards / float(spend_total), 1) if spend_total else None,
        spend_this_month=Decimal(session.scalar(select(func.coalesce(func.sum(cost), 0)).where(SpendLedger.occurred_at >= month_start))),
        caps=[(row.provider, row.period, row.money_cap, row.request_cap, row.currency) for row in policies],
    )


def _collection_section(session: Session) -> CollectionSection:
    outcomes: dict[str, dict[str, int]] = defaultdict(dict)
    for source, outcome, count in session.execute(
        select(FetchRun.source, FetchRun.outcome, func.count()).group_by(FetchRun.source, FetchRun.outcome)
    ).all():
        outcomes[source][outcome] = count
    names = dict(session.execute(select(Employer.id, Employer.name)).all())
    boards = session.scalars(select(EmployerBoard)).all()
    return CollectionSection(
        outcomes_by_source=dict(outcomes),
        repeated_failures=[
            (names.get(board.employer_id, board.board_id), board.consecutive_failures)
            for board in boards
            if board.consecutive_failures >= REPEATED_FAILURES
        ],
        brake_events=[
            event
            for board in boards
            for event in _brake_events(session, board, names.get(board.employer_id, board.board_id))
        ],
        jobs_by_freshness=dict(session.execute(select(Job.freshness, func.count()).group_by(Job.freshness)).all()),
    )


def _brake_events(session: Session, board: EmployerBoard, name: str) -> list[tuple[str, datetime, int, int]]:
    """Every complete fetch where more than half of the jobs on the fetch before it had gone."""
    runs = session.execute(
        select(FetchRun.id, FetchRun.finished_at)
        .where(FetchRun.employer_board_id == board.id, FetchRun.outcome.in_(FULL_READS))
        .order_by(FetchRun.finished_at, FetchRun.id)
    ).all()
    if len(runs) < 2:
        return []
    listed: dict = defaultdict(set)
    for run_id, job_id in session.execute(
        select(RawPayload.fetch_run_id, RawPayload.source_job_id).where(
            RawPayload.fetch_run_id.in_([run.id for run in runs]),
            RawPayload.source_job_id.is_not(None),
        )
    ).all():
        listed[run_id].add(job_id)
    events = []
    for earlier, later in zip(runs, runs[1:], strict=False):
        before = listed[earlier.id]
        vanished = before - listed[later.id]
        if len(before) >= BRAKE_MIN_JOBS and len(vanished) * 2 > len(before):
            events.append((name, later.finished_at, len(vanished), len(before)))
    return events


def _audit_section(session: Session) -> AuditSection:
    rows = session.scalars(select(AuditSample)).all()
    if not rows:
        return AuditSection(0, None, 0, 0, 0, 0)
    latest = max(row.created_at.date() for row in rows)
    newest = [row for row in rows if row.created_at.date() == latest]
    judged = [row for row in newest if row.is_correct is not None]
    useful = [row for row in newest if row.is_useful is not None]
    return AuditSection(
        sampled=len(newest),
        latest_day=latest.isoformat(),
        judged=len(judged),
        correct=sum(1 for row in judged if row.is_correct),
        useful_judged=len(useful),
        useful=sum(1 for row in useful if row.is_useful),
    )
