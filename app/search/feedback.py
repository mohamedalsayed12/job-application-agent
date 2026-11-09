"""Measure what each search run led to, for the planner to act on (MVP section 8, T8, T9).

Every figure is worked out again from the stored rows, so this is safe to repeat.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.matching.match import (
    internship_seekers,
    location_fit,
    match_role,
    wanted_locations,
)
from app.models import (
    ApplicantPreference,
    Candidate,
    CandidateEvidence,
    EmployerBoard,
    JobFreshness,
    JobLink,
    JobPosting,
    SearchResult,
    SearchRun,
    SearchTemplate,
    SearchTemplateStatus,
    SpendLedger,
)
from app.quality.checks import JobFacts, JobQuality
from app.quality.report import check_all
from app.search.runner import template_stats_row
from app.search.templates import UNPROVEN_RUN_THRESHOLD

_NEVER = datetime.min.replace(tzinfo=UTC)
_NOTHING_STALE = Decimal(1)


@dataclass(frozen=True, slots=True)
class Sighting:
    """One piece of evidence. Seed-list and manual evidence has no run."""

    candidate_id: uuid.UUID
    at: datetime
    run_id: uuid.UUID | None
    planned_at: datetime | None


@dataclass(slots=True)
class RunOutcome:
    candidates: Decimal = Decimal(0)
    boards: Decimal = Decimal(0)
    relevant: Decimal = Decimal(0)
    relevant_open: Decimal = Decimal(0)


def first_finders(sightings: list[Sighting]) -> set[uuid.UUID]:
    """The runs of the plan that found this first. Found first by anything else credits no run."""
    if not sightings:
        return set()
    first = min(sightings, key=lambda s: (s.at, s.run_id is not None, s.planned_at or _NEVER))
    if first.run_id is None:
        return set()
    return {s.run_id for s in sightings if s.run_id is not None and s.planned_at == first.planned_at}


def _is_relevant(facts: JobFacts, quality: JobQuality, preference: ApplicantPreference, *, internship: bool) -> bool:
    """Role and location only, whatever the job's state: closed jobs must lower freshness."""
    if not quality.unique or facts.sector != preference.sector:
        return False
    if match_role(facts.title, facts.description, preference.target_roles, internship=internship) is None:
        return False
    fits, _ = location_fit(wanted_locations(preference.locations), facts.places, facts.remote_type)
    return fits


def _sightings(session: Session) -> dict[uuid.UUID, list[Sighting]]:
    rows = session.execute(
        select(CandidateEvidence.candidate_id, CandidateEvidence.created_at, SearchRun.id, SearchRun.created_at)
        .outerjoin(SearchResult, SearchResult.id == CandidateEvidence.search_result_id)
        .outerjoin(SearchRun, SearchRun.id == SearchResult.search_run_id)
    ).all()
    by_candidate: dict[uuid.UUID, list[Sighting]] = defaultdict(list)
    for row in rows:
        by_candidate[row[0]].append(Sighting(*row))
    return by_candidate


def _board_credits(
    session: Session, by_candidate: dict[uuid.UUID, list[Sighting]]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]]:
    """Per run, the boards it first found and its share of each.

    Only evidence from before a board was verified counts, so re-finding a board
    already in the directory adds nothing (T8).
    """
    on_board: dict[tuple[str, str, str], list[Sighting]] = defaultdict(list)
    candidates = session.execute(
        select(Candidate.id, Candidate.board_system, Candidate.board_instance, Candidate.board_id).where(
            Candidate.board_id.is_not(None)
        )
    ).all()
    for candidate_id, system, instance, board_id in candidates:
        on_board[(system, instance, board_id)].extend(by_candidate.get(candidate_id, []))

    credits: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]] = defaultdict(list)
    for board in session.scalars(select(EmployerBoard).where(EmployerBoard.verified_at.is_not(None))):
        sightings = on_board.get((board.system, board.instance, board.board_id), [])
        finders = first_finders([s for s in sightings if s.at < board.verified_at])
        for run_id in finders:
            credits[run_id].append((board.id, Decimal(1) / len(finders)))
    return credits


def _add_job_outcomes(
    session: Session,
    *,
    now: datetime,
    runs: list[SearchRun],
    credits: dict[uuid.UUID, list[tuple[uuid.UUID, Decimal]]],
    outcomes: dict[uuid.UUID, RunOutcome],
) -> None:
    jobs_on_board: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for board_id, job_id in session.execute(
        select(JobPosting.employer_board_id, JobLink.job_id).join(JobLink, JobLink.job_posting_id == JobPosting.id)
    ):
        jobs_on_board[board_id].add(job_id)
    results = {facts.job_id: (facts, quality) for facts, quality in check_all(session, now)}
    preferences = {p.applicant_id: p for p in session.scalars(select(ApplicantPreference))}
    seekers = internship_seekers(session)

    for run in runs:
        preference = preferences.get(run.applicant_id)
        if preference is None:
            continue
        for board_id, share in credits.get(run.id, []):
            for job_id in jobs_on_board[board_id]:
                facts, quality = results[job_id]
                if not _is_relevant(facts, quality, preference, internship=run.applicant_id in seekers):
                    continue
                outcomes[run.id].relevant += share
                if facts.freshness == JobFreshness.open and quality.recent:
                    outcomes[run.id].relevant_open += share


def record_outcomes(session: Session, *, now: datetime) -> dict[str, RunOutcome]:
    """Store each finished run's outcomes and each template's totals. Returns the totals."""
    by_candidate = _sightings(session)
    outcomes: dict[uuid.UUID, RunOutcome] = defaultdict(RunOutcome)
    for sightings in by_candidate.values():
        finders = first_finders(sightings)
        for run_id in finders:
            outcomes[run_id].candidates += Decimal(1) / len(finders)

    credits = _board_credits(session, by_candidate)
    for run_id, boards in credits.items():
        outcomes[run_id].boards = sum((share for _, share in boards), Decimal(0))

    runs = list(session.scalars(select(SearchRun).where(SearchRun.finished_at.is_not(None))))
    if credits:
        _add_job_outcomes(session, now=now, runs=runs, credits=credits, outcomes=outcomes)

    totals: dict[str, RunOutcome] = defaultdict(RunOutcome)
    for run in runs:
        outcome = outcomes[run.id]
        run.new_candidates = outcome.candidates
        run.new_verified_boards = outcome.boards
        run.relevant_jobs = outcome.relevant
        run.freshness_share = outcome.relevant_open / outcome.relevant if outcome.relevant else _NOTHING_STALE
        run.outcomes_at = now
        total = totals[run.template_id]
        total.candidates += outcome.candidates
        total.boards += outcome.boards
        total.relevant += outcome.relevant
        total.relevant_open += outcome.relevant_open

    cost = func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)
    spent = dict(
        session.execute(
            select(SearchRun.template_id, func.sum(cost))
            .join(SpendLedger, SpendLedger.related_run_id == SearchRun.id)
            .group_by(SearchRun.template_id)
        ).all()
    )
    for template_id, total in totals.items():
        stats = template_stats_row(session, template_id, now=now)
        stats.total_cost = Decimal(spent.get(template_id, 0))
        stats.new_candidates = total.candidates
        stats.new_verified_boards = total.boards
        stats.relevant_jobs = total.relevant
        stats.freshness_share = total.relevant_open / total.relevant if total.relevant else _NOTHING_STALE
        stats.updated_at = now
        template = session.get(SearchTemplate, template_id)
        if int(stats.run_count or 0) >= UNPROVEN_RUN_THRESHOLD and template.status == SearchTemplateStatus.unproven:
            template.status = SearchTemplateStatus.active
    session.flush()
    return dict(totals)
