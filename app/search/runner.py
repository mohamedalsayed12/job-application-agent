"""Execute one search_run through budget gate + fetch gateway; record status/results/cost."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings, get_settings
from app.fetch.gateway import FetchGateway
from app.models import (
    SearchResult,
    SearchRun,
    SearchRunStatus,
    SpendKind,
    TemplateStats,
)
from app.search.mojeek import search_mojeek
from app.search.queries import GREENHOUSE_SITE, LEVER_SITES
from app.search.reed import search_reed
from app.search.types import ProviderFailure, ProviderSuccess, RunTerminal

# D12: search API retries after 1, 5, 30 minutes (attempts 1..3).
_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunTerminal
    result_count: int
    cost_amount: Decimal | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SearchBudgetWait:
    """Run stays planned / non-failed when a money or request cap blocks (T3)."""

    cap: str


async def run_search(
    session: Session,
    gateway: FetchGateway,
    run: SearchRun,
    *,
    settings: Settings | None = None,
    clock: Callable[[], datetime] | None = None,
    site: str | None = None,
) -> RunOutcome | SearchBudgetWait:
    """Claim ``planned``/retryable ``failed`` → ``running``, call provider, settle terminal state.

    Cost is written on the run and into ``template_stats`` as soon as the call settles (T9).
    Verification / job outcomes land in later stages when known.
    """
    cfg = settings or get_settings()
    now_fn = clock or (lambda: datetime.now(UTC))
    now = now_fn()

    if run.status not in {SearchRunStatus.planned, SearchRunStatus.failed}:
        raise ValueError(f"search_run {run.id} not claimable (status={run.status})")

    run.status = SearchRunStatus.running
    run.claimed_at = now
    run.started_at = now
    run.attempt_count = int(run.attempt_count or 0) + 1
    run.last_error = None
    session.flush()

    provider = run.provider.lower()
    units = Decimal("1")
    cost = estimate_cost(
        provider=provider,
        kind=SpendKind.search,
        units=units,
        settings=cfg,
    )
    gate = BudgetGate(session, clock=now_fn)
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider=provider,
                kind=SpendKind.search,
                units=units,
                unit_label="request",
                estimated_cost=cost,
                related_run_id=run.id,
            )
        )
    except BudgetWait as exc:
        _revert_to_planned(run, now=now)
        session.flush()
        return SearchBudgetWait(cap=exc.cap)

    session.flush()
    resolved_site = site if site is not None else _site_for_template(run.template_id)

    try:
        outcome = await _call_provider(
            gateway,
            provider=provider,
            query=run.query,
            site=resolved_site,
            settings=cfg,
        )
    except Exception as exc:  # noqa: BLE001 — never leave a run stuck in running
        gate.settle(reservation, actual_cost=Decimal("0"), consumed=False)
        return _finish_failed(
            session,
            run,
            now=now_fn(),
            error=f"unexpected:{type(exc).__name__}",
            cost_amount=Decimal("0"),
        )

    if isinstance(outcome, ProviderFailure):
        if outcome.reason == "no_usable_urls":
            # Provider answered, but nothing extractable — bill the request (T9).
            gate.settle(reservation, actual_cost=cost, units=units, consumed=True)
            return _finish_terminal(
                session,
                run,
                status=SearchRunStatus.inconclusive,
                now=now_fn(),
                result_count=0,
                cost_amount=cost,
                error=outcome.reason,
                schedule_retry=False,
            )
        gate.settle(reservation, actual_cost=Decimal("0"), consumed=False)
        return _finish_failed(
            session,
            run,
            now=now_fn(),
            error=outcome.reason,
            cost_amount=Decimal("0"),
            schedule_retry=outcome.retryable,
        )

    assert isinstance(outcome, ProviderSuccess)
    gate.settle(reservation, actual_cost=cost, units=units, consumed=True)
    for rank, hit in enumerate(outcome.hits, start=1):
        session.add(
            SearchResult(
                search_run_id=run.id,
                rank=rank,
                url=hit.url,
                title=hit.title,
                snippet=hit.snippet,
            )
        )
    return _finish_terminal(
        session,
        run,
        status=SearchRunStatus.succeeded,
        now=now_fn(),
        result_count=len(outcome.hits),
        cost_amount=cost,
        error=None,
        schedule_retry=False,
    )


def _site_for_template(template_id: str) -> str | None:
    if template_id == "BD1":
        return GREENHOUSE_SITE
    if template_id == "BD2":
        return LEVER_SITES[0]
    return None


async def _call_provider(
    gateway: FetchGateway,
    *,
    provider: str,
    query: str,
    site: str | None,
    settings: Settings,
):
    if provider == "reed":
        return await search_reed(gateway, keywords=query, settings=settings)
    if provider == "adzuna":
        return ProviderFailure(reason="adzuna_disabled", retryable=False)
    if provider == "mojeek" or provider == settings.web_search_provider:
        return await search_mojeek(
            gateway,
            query=query,
            site=site,
            settings=settings,
        )
    return ProviderFailure(reason=f"unknown_provider:{provider}", retryable=False)


def _revert_to_planned(run: SearchRun, *, now: datetime) -> None:
    run.status = SearchRunStatus.planned
    run.claimed_at = None
    run.started_at = None
    run.attempt_count = max(0, int(run.attempt_count or 0) - 1)
    run.next_attempt_at = now


def _finish_failed(
    session: Session,
    run: SearchRun,
    *,
    now: datetime,
    error: str,
    cost_amount: Decimal,
    schedule_retry: bool = True,
) -> RunOutcome:
    attempts = int(run.attempt_count or 0)
    if schedule_retry and attempts < _MAX_ATTEMPTS:
        delay = _RETRY_DELAYS[min(attempts, len(_RETRY_DELAYS)) - 1]
        return _finish_terminal(
            session,
            run,
            status=SearchRunStatus.failed,
            now=now,
            result_count=0,
            cost_amount=cost_amount,
            error=error,
            schedule_retry=True,
            retry_at=now + delay,
        )
    return _finish_terminal(
        session,
        run,
        status=SearchRunStatus.failed,
        now=now,
        result_count=0,
        cost_amount=cost_amount,
        error=error,
        schedule_retry=False,
    )


def _finish_terminal(
    session: Session,
    run: SearchRun,
    *,
    status: str,
    now: datetime,
    result_count: int,
    cost_amount: Decimal,
    error: str | None,
    schedule_retry: bool,
    retry_at: datetime | None = None,
) -> RunOutcome:
    run.status = status
    run.finished_at = now
    run.claimed_at = None
    run.result_count = result_count
    run.cost_amount = cost_amount
    run.last_error = error
    run.next_attempt_at = retry_at if schedule_retry else None
    _bump_template_stats(session, run, cost_amount=cost_amount, now=now)
    session.flush()
    terminal: RunTerminal
    if status == SearchRunStatus.succeeded:
        terminal = "succeeded"
    elif status == SearchRunStatus.inconclusive:
        terminal = "inconclusive"
    else:
        terminal = "failed"
    return RunOutcome(
        status=terminal,
        result_count=result_count,
        cost_amount=cost_amount,
        error=error,
    )


def _bump_template_stats(
    session: Session,
    run: SearchRun,
    *,
    cost_amount: Decimal,
    now: datetime,
) -> None:
    """Record run_count and cost from the first run (T9). Yields fill in later stages."""
    stats = session.get(TemplateStats, run.template_id)
    if stats is None:
        stats = TemplateStats(
            template_id=run.template_id,
            run_count=0,
            total_cost=Decimal("0"),
            new_candidates=0,
            new_verified_boards=0,
            relevant_jobs=0,
            freshness_share=Decimal("0"),
            updated_at=now,
        )
        session.add(stats)
    stats.run_count = int(stats.run_count or 0) + 1
    stats.total_cost = Decimal(stats.total_cost or 0) + cost_amount
    stats.updated_at = now
