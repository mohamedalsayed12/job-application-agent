"""Stage 9: turn one fetch run's raw jobs into postings and master jobs, and close jobs no search finds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.cleaning.linking import (
    FIRST_POSTING_REASON,
    is_primary,
    link_posting,
    mirror_posting,
)
from app.cleaning.normalize import NormalizedJob, normalize_job
from app.models import (
    EmployerType,
    FetchOutcome,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    RawPayload,
)

FULL_READS = (FetchOutcome.complete, FetchOutcome.empty)

COMPLETENESS_FIELDS = (
    "title",
    "apply_url",
    "description",
    "location_city",
    "location_country",
    "remote_type",
    "salary",
    "hours",
    "contract_type",
    "posted_at",
    "closes_at",
)


@dataclass(slots=True)
class CleanReport:
    postings_new: int = 0
    postings_updated: int = 0
    unusable: int = 0
    errors: list[str] = field(default_factory=list)


def clean_run(session: Session, run: FetchRun) -> CleanReport:
    """Safe to repeat, and safe on an older run: older data never replaces newer data."""
    report = CleanReport()
    complete = run.outcome in FULL_READS
    raws = session.scalars(
        select(RawPayload).where(RawPayload.fetch_run_id == run.id).order_by(RawPayload.source_job_id)
    ).all()
    for raw in raws:
        job = normalize_job(raw.source, raw.payload)
        if job is None or raw.source_job_id is None:
            report.unusable += 1
            continue
        _store_posting(session, raw, job, complete, report)
    return report


def _store_posting(
    session: Session,
    raw: RawPayload,
    job: NormalizedJob,
    complete: bool,
    report: CleanReport,
) -> None:
    posting = session.scalar(
        select(JobPosting).where(JobPosting.source == raw.source, JobPosting.source_job_id == raw.source_job_id)
    )
    is_new = posting is None
    if is_new:
        posting = JobPosting(
            source=raw.source,
            source_job_id=raw.source_job_id,
            employer_type=EmployerType.direct,
            first_seen_at=raw.fetched_at,
            last_seen_at=raw.fetched_at,
        )
        session.add(posting)
    is_latest = is_new or raw.fetched_at >= posting.last_seen_at
    if is_latest:
        _copy_fields(posting, raw, job)
        posting.last_seen_at = raw.fetched_at
    posting.first_seen_at = min(posting.first_seen_at, raw.fetched_at)
    if complete and (
        posting.last_seen_complete_fetch_at is None or raw.fetched_at > posting.last_seen_complete_fetch_at
    ):
        posting.last_seen_complete_fetch_at = raw.fetched_at
    session.flush()

    link = link_posting(session, posting)
    master = session.get(Job, link.job_id)
    master.first_seen_at = min(master.first_seen_at, posting.first_seen_at)
    master.last_seen_at = max(master.last_seen_at, posting.last_seen_at)
    if is_latest and is_primary(master, posting):
        mirror_posting(master, posting)
    if is_latest and master.freshness != JobFreshness.open:
        # A search found it again, so it is open again.
        master.freshness = JobFreshness.open
        master.closure_evidence = None
    if is_new:
        report.postings_new += 1
    else:
        report.postings_updated += 1


def _copy_fields(posting: JobPosting, raw: RawPayload, job: NormalizedJob) -> None:
    posting.raw_payload_id = raw.id
    posting.title = job.title
    posting.company_name = job.company
    posting.location_city = job.location.city
    posting.location_country = job.location.country
    posting.remote_type = job.location.remote_type
    posting.places = [{"city": place.city, "country": place.country, "region": place.region} for place in job.places]
    posting.salary_min = job.salary.minimum
    posting.salary_max = job.salary.maximum
    posting.salary_currency = job.salary.currency
    posting.salary_period = job.salary.period
    posting.salary_known = job.salary.known
    posting.hours = job.hours
    posting.contract_type = job.contract_type
    posting.description = job.description
    posting.apply_url = job.apply_url
    posting.posted_at = job.posted_at
    posting.closes_at = job.closes_at


def reclean_all(session: Session) -> CleanReport:
    """Replay every stored run oldest first, to pick up runs that missed cleaning or a parser change."""
    total = CleanReport()
    for run in session.scalars(select(FetchRun).order_by(FetchRun.finished_at)).all():
        run_id = run.id
        try:
            with session.begin_nested():
                report = clean_run(session, run)
        except Exception as exc:  # One bad run must not stop the replay of every other run.
            total.errors.append(f"run {run_id}: {type(exc).__name__}: {exc}"[:300])
            continue
        total.postings_new += report.postings_new
        total.postings_updated += report.postings_updated
        total.unusable += report.unusable
    return total


def close_stale(session: Session, *, source: str, now: datetime, after: timedelta) -> int:
    """A search shows only recent jobs, so a job no search has found for a while is taken as closed."""
    stale = (
        select(JobLink.job_id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobPosting.source == source)
        .group_by(JobLink.job_id)
        .having(func.max(JobPosting.last_seen_at) < now - after)
    )
    closed = session.execute(
        update(Job)
        .where(Job.id.in_(stale), Job.freshness != JobFreshness.closed)
        .values(freshness=JobFreshness.closed, closure_evidence={"rule": f"not_found_by_any_search_for_{after.days}_days"})
        .execution_options(synchronize_session=False)
    )
    return closed.rowcount or 0


def completeness(session: Session) -> dict[str, dict[str, float]]:
    """Share of postings with each field filled in, per source. Salary counts only when known."""
    results: dict[str, dict[str, float]] = {}
    sources = session.execute(select(JobPosting.source, func.count()).group_by(JobPosting.source)).all()
    for source, total in sources:
        results[source] = {"postings": total}
        for name in COMPLETENESS_FIELDS:
            condition = JobPosting.salary_known if name == "salary" else getattr(JobPosting, name).is_not(None)
            filled = session.scalar(select(func.count()).where(JobPosting.source == source, condition))
            results[source][name] = round(100 * filled / total, 1)
    return results


def master_job_counts(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Job.freshness, func.count()).group_by(Job.freshness)).all())


def link_method_counts(session: Session) -> dict[str, int]:
    """First postings are stored as exact_job_id but matched nothing, so they are counted apart."""
    first = select(func.count()).where(JobLink.reason == FIRST_POSTING_REASON)
    matched = (
        select(JobLink.method, func.count())
        .where(JobLink.reason != FIRST_POSTING_REASON)
        .group_by(JobLink.method)
    )
    return {"first_posting": session.scalar(first), **dict(session.execute(matched).all())}
