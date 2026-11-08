"""Three-state freshness, worked out from stored fetch runs rather than running counters.

Reading the evidence each time means a crash or a missed cleaning pass cannot close a
job: a job closes only when the raw jobs of two complete fetches show it missing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EmployerBoard,
    FetchOutcome,
    FetchRun,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    RawPayload,
)

FULL_READS = (FetchOutcome.complete, FetchOutcome.empty)
FAILURES_BEFORE_UNKNOWN = 3
BRAKE_MIN_JOBS = 5
CLOSING_RULE = "missing_from_2_consecutive_complete_fetches"


@dataclass(slots=True)
class FreshnessReport:
    closed: int = 0
    reopened: int = 0
    unknown: int = 0
    held: int = 0
    brake_on: bool = False


@dataclass(slots=True)
class _BoardView:
    latest: FetchRun | None
    previous: FetchRun | None
    latest_ids: set[str]
    previous_ids: set[str]
    unhealthy: bool
    brake_on: bool = field(init=False)

    def __post_init__(self) -> None:
        vanished = self.previous_ids - self.latest_ids
        self.brake_on = (
            self.latest is not None
            and len(self.previous_ids) >= BRAKE_MIN_JOBS
            and len(vanished) * 2 > len(self.previous_ids)
        )

    def missed_twice(self, posting: JobPosting) -> bool:
        return (
            self.latest is not None
            and self.previous is not None
            and posting.source_job_id not in self.latest_ids
            and posting.source_job_id not in self.previous_ids
            and posting.last_seen_at < self.previous.finished_at
        )

    def seen_lately(self, posting: JobPosting) -> bool:
        """Listed in the latest complete fetch, or in a partial read since then."""
        return (
            self.latest is None
            or posting.source_job_id in self.latest_ids
            or posting.last_seen_at > self.latest.finished_at
        )

    def state(self, posting: JobPosting, *, already_closed: bool) -> str:
        if already_closed and not self.seen_lately(posting):
            return JobFreshness.closed
        if self.missed_twice(posting) and not self.brake_on:
            return JobFreshness.closed
        return JobFreshness.unknown if self.unhealthy else JobFreshness.open


def refresh_board_freshness(session: Session, board: EmployerBoard, *, now: datetime) -> FreshnessReport:
    """Safe to repeat: the same fetch runs always give the same states."""
    report = FreshnessReport()
    views: dict[uuid.UUID, _BoardView] = {board.id: _view(session, board)}
    report.brake_on = views[board.id].brake_on

    linked = session.execute(
        select(JobLink.job_id).join(JobPosting, JobPosting.id == JobLink.job_posting_id).where(
            JobPosting.employer_board_id == board.id
        )
    ).scalars().all()
    job_ids = set(linked)
    if not job_ids:
        return report

    postings_by_job: dict[uuid.UUID, list[JobPosting]] = {}
    for job_id, posting in session.execute(
        select(JobLink.job_id, JobPosting)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobLink.job_id.in_(job_ids))
    ).all():
        postings_by_job.setdefault(job_id, []).append(posting)

    for job in session.scalars(select(Job).where(Job.id.in_(job_ids))).all():
        _update_job(session, job, postings_by_job[job.id], views, now, report)
    session.flush()
    return report


def _update_job(
    session: Session,
    job: Job,
    postings: list[JobPosting],
    views: dict[uuid.UUID, _BoardView],
    now: datetime,
    report: FreshnessReport,
) -> None:
    already_closed = job.freshness == JobFreshness.closed
    states = []
    for posting in postings:
        if posting.employer_board_id is None:
            # A search source says nothing by leaving a job out; only an expiry date closes it.
            expired = posting.closes_at is not None and posting.closes_at <= now
            states.append(JobFreshness.closed if expired else JobFreshness.open)
            continue
        view = views.get(posting.employer_board_id)
        if view is None:
            view = _view(session, session.get(EmployerBoard, posting.employer_board_id))
            views[posting.employer_board_id] = view
        states.append(view.state(posting, already_closed=already_closed))
        if view.brake_on and view.missed_twice(posting) and not already_closed:
            report.held += 1

    if JobFreshness.open in states:
        new_state = JobFreshness.open
    elif all(state == JobFreshness.closed for state in states):
        new_state = JobFreshness.closed
    else:
        new_state = JobFreshness.unknown

    if new_state == job.freshness:
        return
    if new_state == JobFreshness.closed:
        job.closure_evidence = _evidence(postings, views, now)
        report.closed += 1
    else:
        job.closure_evidence = None
        if already_closed:
            report.reopened += 1
    if new_state == JobFreshness.unknown:
        report.unknown += 1
    job.freshness = new_state


def _evidence(postings: list[JobPosting], views: dict[uuid.UUID, _BoardView], now: datetime) -> dict:
    runs: dict[str, str] = {}
    for posting in postings:
        view = views.get(posting.employer_board_id) if posting.employer_board_id else None
        if view is not None and view.latest is not None and view.previous is not None:
            runs[str(view.latest.id)] = view.latest.finished_at.isoformat()
            runs[str(view.previous.id)] = view.previous.finished_at.isoformat()
    return {"rule": CLOSING_RULE, "fetch_runs": runs, "closed_at": now.isoformat()}


def _view(session: Session, board: EmployerBoard) -> _BoardView:
    full_reads = session.scalars(
        select(FetchRun)
        .where(FetchRun.employer_board_id == board.id, FetchRun.outcome.in_(FULL_READS))
        .order_by(FetchRun.finished_at.desc(), FetchRun.id)
        .limit(2)
    ).all()
    latest = full_reads[0] if full_reads else None
    previous = full_reads[1] if len(full_reads) > 1 else None
    last_outcome = session.scalar(
        select(FetchRun.outcome)
        .where(FetchRun.employer_board_id == board.id)
        .order_by(FetchRun.finished_at.desc(), FetchRun.id)
        .limit(1)
    )
    return _BoardView(
        latest=latest,
        previous=previous,
        latest_ids=_job_ids(session, latest),
        previous_ids=_job_ids(session, previous),
        unhealthy=(
            board.consecutive_failures >= FAILURES_BEFORE_UNKNOWN or last_outcome == FetchOutcome.not_found
        ),
    )


def _job_ids(session: Session, run: FetchRun | None) -> set[str]:
    if run is None:
        return set()
    return set(
        session.scalars(
            select(RawPayload.source_job_id).where(
                RawPayload.fetch_run_id == run.id,
                RawPayload.source_job_id.is_not(None),
            )
        ).all()
    )
