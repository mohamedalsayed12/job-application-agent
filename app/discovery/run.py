"""One discovery pass: search, extract, find official domains (CF3), verify.

Every step commits through ``persist`` so a crash or budget stop leaves the
database consistent, and running the pass again picks up where it stopped.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applicants.discovery import to_discovery_preference
from app.core.config import Settings, get_settings
from app.discovery.boards import attach_public_board
from app.discovery.cf3 import search_official_domain
from app.extraction import extract_ai_leftovers, extract_and_persist_search_results
from app.fetch.gateway import FetchGateway
from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    ApplicantPreference,
    Candidate,
    CandidateEvidence,
    CandidateState,
    SearchResult,
    SearchRun,
    SearchRunStatus,
)
from app.queue import claim_next_candidate
from app.search import (
    SearchBudgetWait,
    create_runs_from_plan,
    ensure_search_templates,
    plan_cold_start,
    run_search,
)
from app.verify import verify_candidate


@dataclass(slots=True)
class DiscoveryReport:
    searches: Counter[str] = field(default_factory=Counter)
    code_candidates: int = 0
    cf3: Counter[str] = field(default_factory=Counter)
    boards_attached: int = 0
    verified: Counter[str] = field(default_factory=Counter)
    stopped: str = ""

    def lines(self) -> list[str]:
        return [
            f"searches: {dict(self.searches)}",
            f"code extraction touched {self.code_candidates} candidates",
            f"cf3: {dict(self.cf3)}",
            f"boards attached: {self.boards_attached}",
            f"verification: {dict(self.verified)}",
            *([f"stopped early: {self.stopped}"] if self.stopped else []),
        ]


async def run_discovery(
    session: Session,
    gateway: FetchGateway,
    *,
    slots_per_applicant: int = 5,
    max_cf3: int = 60,
    max_verify: int = 200,
    settings: Settings | None = None,
    persist: Callable[[], None] | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> DiscoveryReport:
    cfg = settings or get_settings()
    commit = persist or session.flush
    report = DiscoveryReport()
    ensure_search_templates(session, cfg)
    commit()

    prefs = {
        p.applicant_id: to_discovery_preference(p) for p in session.scalars(select(ApplicantPreference))
    }
    new_runs: list[SearchRun] = []
    for applicant_id, pref in prefs.items():
        planned = _planned_runs(session, applicant_id)
        now = datetime.now(UTC)
        runs = [r for r in planned if r.next_attempt_at is None or r.next_attempt_at <= now]
        if planned and not runs:
            report.searches["not_due"] += len(planned)
            log(f"search applicant {applicant_id}: {len(planned)} runs wait for their retry time")
            continue
        if not planned:
            plan = plan_cold_start(session, pref, total_slots=slots_per_applicant, settings=cfg)
            runs = create_runs_from_plan(session, plan, applicant_id=applicant_id)
            commit()
        for run in runs:
            outcome = await run_search(session, gateway, run, settings=cfg, persist=commit)
            commit()
            if isinstance(outcome, SearchBudgetWait):
                # Runs stay planned; the next pass runs them before planning new ones.
                report.searches[f"wait:{outcome.cap}"] += 1
                log(f"search {run.template_id:4} applicant {applicant_id} -> wait {outcome.cap}")
                break
            report.searches[str(run.status)] += 1
            log(f"search {run.template_id:4} {run.query[:50]!r} -> {run.status}")
            new_runs.append(run)
        if report.searches.get("wait:monthly") or report.searches.get("wait:daily"):
            report.stopped = "search money cap"
            break

    for run in new_runs:
        if run.status != SearchRunStatus.succeeded:
            continue
        rows = list(session.scalars(select(SearchResult).where(SearchResult.search_run_id == run.id)))
        report.code_candidates += len(extract_and_persist_search_results(session, rows))
        commit()
        outcome = await extract_ai_leftovers(
            session, gateway, rows, settings=cfg, related_run_id=run.id, persist=commit
        )
        commit()
        log(f"ai   {run.template_id:4} -> {type(outcome).__name__}")

    for candidate in _waiting_for_cf3(session)[:max_cf3]:
        applicant_id = _applicant_for(session, candidate) or next(iter(prefs))
        outcome = await search_official_domain(
            session, gateway, candidate, applicant_id=applicant_id, pref=prefs[applicant_id],
            settings=cfg, persist=commit,
        )
        commit()
        report.cf3[outcome.status] += 1
        log(f"cf3  {candidate.company_name[:40]:40} -> {outcome.status} {outcome.domains}")
        if outcome.status == "budget_wait" and outcome.detail in ("monthly", "daily"):
            report.stopped = f"cf3 money cap: {outcome.detail}"
            break

    await lookup_and_verify(session, gateway, report=report, max_verify=max_verify, commit=commit, log=log)
    return report


async def lookup_and_verify(
    session: Session,
    gateway: FetchGateway,
    *,
    report: DiscoveryReport,
    max_verify: int,
    commit: Callable[[], None],
    log: Callable[[str], None],
) -> None:
    """Free steps of a pass: board lookup, then verify the queue. No search spend."""
    for candidate in _without_board(session):
        found = await attach_public_board(session, gateway, candidate)
        commit()
        if found is not None:
            report.boards_attached += 1
            log(f"board {candidate.company_name[:40]:40} -> {found.system}:{found.board_id}")

    for _ in range(max_verify):
        candidate = claim_next_candidate(session)
        commit()
        if candidate is None:
            break
        try:
            decision = await verify_candidate(session, candidate, gateway)
        except Exception as exc:  # one bad site must not end the pass; the claim expires
            session.rollback()
            report.verified["error"] += 1
            log(f"verify {candidate.company_name[:40]:40} !! {type(exc).__name__}: {exc}")
            continue
        commit()
        report.verified[decision.state] += 1
        log(f"verify {candidate.company_name[:40]:40} -> {decision.state} ({decision.reason})")


def _planned_runs(session: Session, applicant_id: uuid.UUID) -> list[SearchRun]:
    """Cold-start runs a budget wait left planned (CF3 runs are made per candidate)."""
    return list(
        session.scalars(
            select(SearchRun)
            .where(
                SearchRun.applicant_id == applicant_id,
                SearchRun.status == SearchRunStatus.planned,
                SearchRun.template_id != "CF3",
            )
            .order_by(SearchRun.created_at)
        )
    )


def _without_board(session: Session) -> list[Candidate]:
    """Candidates the board lookup can still help; a claimed row is left alone."""
    return list(
        session.scalars(
            select(Candidate)
            .where(
                Candidate.board_id.is_(None),
                Candidate.state.in_(
                    (
                        CandidateState.queued,
                        CandidateState.retry_wait,
                        CandidateState.needs_review,
                        CandidateState.unsupported,
                    )
                ),
            )
            .order_by(Candidate.created_at)
        )
    )


def _waiting_for_cf3(session: Session) -> list[Candidate]:
    """Live candidates with no domain that CF3 has not searched yet (R2)."""
    return list(
        session.scalars(
            select(Candidate)
            .where(
                Candidate.state.not_in(CANDIDATE_LIVE_UNIQUE_EXCLUDES),
                Candidate.primary_domain.is_(None),
                func.cardinality(Candidate.domain_candidates) == 0,
                Candidate.domain_searched_at.is_(None),
            )
            .order_by(Candidate.created_at)
        )
    )


def _applicant_for(session: Session, candidate: Candidate) -> uuid.UUID | None:
    """Applicant whose search produced this candidate's first evidence."""
    return session.scalar(
        select(SearchRun.applicant_id)
        .join(SearchResult, SearchResult.search_run_id == SearchRun.id)
        .join(CandidateEvidence, CandidateEvidence.search_result_id == SearchResult.id)
        .where(CandidateEvidence.candidate_id == candidate.id)
        .limit(1)
    )

