"""Execute one search_run through budget gate + fetch gateway; record status/results/cost."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import select
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
from app.search.reed import search_reed
from app.search.serpapi import search_serpapi
from app.search.templates import get_template_spec
from app.search.types import (
    ProviderFailure,
    ProviderInconclusive,
    ProviderSuccess,
    RunTerminal,
    SearchHit,
)

# D12: retries after 1, 5, 30 minutes → up to 4 attempts (initial + 3 retries).
_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
_BUDGET_BACKOFF = {
    "monthly": timedelta(hours=1),
    "per_run": timedelta(minutes=15),
    "per_applicant": timedelta(hours=1),
}


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


def _is_due(run: SearchRun, *, now: datetime) -> bool:
    if run.status == SearchRunStatus.planned:
        return run.next_attempt_at is None or run.next_attempt_at <= now
    if run.status == SearchRunStatus.failed:
        if run.next_attempt_at is None:
            return False
        if int(run.attempt_count or 0) > len(_RETRY_DELAYS):
            return False
        return run.next_attempt_at <= now
    return False


async def run_search(
    session: Session,
    gateway: FetchGateway,
    run: SearchRun,
    *,
    settings: Settings | None = None,
    clock: Callable[[], datetime] | None = None,
    location: str | None = None,
    site: str | None = None,
    persist: Callable[[], None] | None = None,
) -> RunOutcome | SearchBudgetWait:
    """Run a due ``planned``/retryable ``failed`` search through budget + fetch.

    Production workers should pass ``persist=session.commit`` so the spend hold
    survives a crash across the outside call (budget gate contract). Tests leave
    the default ``flush`` so the outer pytest transaction can roll back.
    """
    cfg = settings or get_settings()
    now_fn = clock or (lambda: datetime.now(UTC))
    now = now_fn()
    persist_fn = persist or session.flush

    locked = session.scalars(
        select(SearchRun).where(SearchRun.id == run.id).with_for_update(skip_locked=True)
    ).first()
    if locked is None:
        raise ValueError(f"search_run {run.id} could not be locked")
    run = locked

    if not _is_due(run, now=now):
        raise ValueError(
            f"search_run {run.id} not due (status={run.status}, "
            f"attempt_count={run.attempt_count}, next_attempt_at={run.next_attempt_at})"
        )

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
        run.status = SearchRunStatus.planned
        run.claimed_at = None
        run.started_at = None
        run.last_error = f"budget_wait:{exc.cap}"
        run.next_attempt_at = now + _BUDGET_BACKOFF.get(exc.cap, timedelta(hours=1))
        session.flush()
        return SearchBudgetWait(cap=exc.cap)

    run.status = SearchRunStatus.running
    run.claimed_at = now
    run.started_at = now
    run.attempt_count = int(run.attempt_count or 0) + 1
    run.last_error = None
    persist_fn()

    spec = get_template_spec(run.template_id)
    resolved_site = site if site is not None else spec.site

    keywords, reed_location = _split_reed_query(run.query, provider=provider)
    try:
        outcome = await _call_provider(
            gateway,
            provider=provider,
            query=keywords if provider == "reed" else run.query,
            location=location if location is not None else reed_location,
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
        gate.settle(reservation, actual_cost=Decimal("0"), consumed=False)
        return _finish_failed(
            session,
            run,
            now=now_fn(),
            error=outcome.reason,
            cost_amount=Decimal("0"),
            schedule_retry=outcome.retryable,
        )

    if isinstance(outcome, ProviderInconclusive):
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

    assert isinstance(outcome, ProviderSuccess)
    gate.settle(reservation, actual_cost=cost, units=units, consumed=True)
    hits = _filter_hits(outcome.hits, site=resolved_site)
    if outcome.hits and not hits:
        return _finish_terminal(
            session,
            run,
            status=SearchRunStatus.inconclusive,
            now=now_fn(),
            result_count=0,
            cost_amount=cost,
            error="no_usable_urls",
            schedule_retry=False,
        )
    for rank, hit in enumerate(hits, start=1):
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
        result_count=len(hits),
        cost_amount=cost,
        error=None,
        schedule_retry=False,
    )


def _split_reed_query(query: str, *, provider: str) -> tuple[str, str | None]:
    """CF1 schedule stores ``role\\tlocation`` so locationName survives without a schema column."""
    if provider != "reed" or "\t" not in query:
        return query, None
    role, loc = query.split("\t", 1)
    return role.strip(), loc.strip() or None


def _filter_hits(hits: tuple[SearchHit, ...], *, site: str | None) -> list[SearchHit]:
    if not site:
        return list(hits)
    needle = site.lower().rstrip(".")
    kept: list[SearchHit] = []
    for hit in hits:
        host = (urlparse(hit.url).hostname or "").lower().rstrip(".")
        if host == needle or host.endswith("." + needle):
            kept.append(hit)
    return kept


async def _call_provider(
    gateway: FetchGateway,
    *,
    provider: str,
    query: str,
    location: str | None,
    site: str | None,
    settings: Settings,
):
    if provider == "reed":
        return await search_reed(
            gateway,
            keywords=query,
            location_name=location,
            settings=settings,
        )
    if provider == "serpapi":
        return await search_serpapi(
            gateway,
            query=query,
            site=site,
            settings=settings,
        )
    if provider == "mojeek":
        return await search_mojeek(
            gateway,
            query=query,
            site=site,
            settings=settings,
        )
    return ProviderFailure(reason=f"unknown_provider:{provider}", retryable=False)


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
    if schedule_retry and attempts <= len(_RETRY_DELAYS):
        delay = _RETRY_DELAYS[attempts - 1]
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
    count_toward_unproven = status in {
        SearchRunStatus.succeeded,
        SearchRunStatus.inconclusive,
    } or not schedule_retry
    _bump_template_stats(
        session,
        run,
        cost_amount=cost_amount,
        now=now,
        count_run=count_toward_unproven,
    )
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
    count_run: bool,
) -> None:
    """Record cost always (T9); bump run_count only on settled outcomes (not mid-retry)."""
    stats = template_stats_row(session, run.template_id, now=now)
    if count_run:
        stats.run_count = int(stats.run_count or 0) + 1
    stats.total_cost = Decimal(stats.total_cost or 0) + cost_amount
    stats.updated_at = now


def template_stats_row(session: Session, template_id: str, *, now: datetime) -> TemplateStats:
    stats = session.get(TemplateStats, template_id)
    if stats is None:
        stats = TemplateStats(
            template_id=template_id,
            run_count=0,
            total_cost=Decimal("0"),
            new_candidates=Decimal("0"),
            new_verified_boards=Decimal("0"),
            relevant_jobs=Decimal("0"),
            freshness_share=Decimal("0"),
            updated_at=now,
        )
        session.add(stats)
    return stats
