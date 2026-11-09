"""Share out search slots from measured outcomes, keeping 20% for exploration (T5, T6, T7, D11)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, floor

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings, get_settings
from app.models import TemplateStats
from app.search.queries import BuiltQuery, build_queries
from app.search.templates import (
    TEMPLATE_SPECS,
    UNPROVEN_RUN_THRESHOLD,
    is_template_enabled,
    resolve_provider,
)

EXPLORATION_SHARE = 0.20
_VALUE_PER_RELEVANT_JOB = Decimal("0.1")
_FREE_SOURCE_REQUESTS = 100


@dataclass(frozen=True, slots=True)
class PlannedSearch:
    template_id: str
    provider: str
    query: str
    location: str | None
    site: str | None
    request_slots: int
    # Slots buy different queries only. An applicant with one role and one location has one query.
    more_queries: tuple[BuiltQuery, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchPlan:
    items: tuple[PlannedSearch, ...]
    total_slots: int
    exploration_slots: int


def template_value(stats: TemplateStats) -> Decimal:
    """New boards + 0.1 x relevant open jobs, all multiplied by the freshness share (MVP section 8)."""
    share = Decimal(stats.freshness_share)
    open_jobs = Decimal(stats.relevant_jobs) * share
    return (Decimal(stats.new_verified_boards) + _VALUE_PER_RELEVANT_JOB * open_jobs) * share


def _weights(proven: Sequence[str], stats: Mapping[str, TemplateStats]) -> dict[str, Decimal]:
    """Value per £1, or per 100 requests for free sources.

    Those two scales have no exchange rate, so each is measured against its own best template.
    """
    rates: dict[str, Decimal] = {}
    for template_id in proven:
        row = stats[template_id]
        cost = Decimal(row.total_cost)
        spent = cost if cost > 0 else Decimal(row.run_count) / _FREE_SOURCE_REQUESTS
        rates[template_id] = template_value(row) / spent
    is_paid = {template_id: Decimal(stats[template_id].total_cost) > 0 for template_id in proven}
    best = {
        paid: max((rates[t] for t in proven if is_paid[t] == paid), default=Decimal(0))
        for paid in (True, False)
    }
    return {t: rates[t] / best[is_paid[t]] if best[is_paid[t]] else Decimal(0) for t in proven}


def _distribute_equal(template_ids: Sequence[str], total_slots: int) -> dict[str, int]:
    if not template_ids or total_slots <= 0:
        return {tid: 0 for tid in template_ids}
    n = len(template_ids)
    base, rem = divmod(total_slots, n)
    return {tid: base + (1 if i < rem else 0) for i, tid in enumerate(template_ids)}


def _distribute_by_weight(weights: Mapping[str, Decimal], total_slots: int) -> dict[str, int]:
    total = sum(weights.values())
    if not total:
        return _distribute_equal(list(weights), total_slots)
    exact = {tid: weight * total_slots / total for tid, weight in weights.items()}
    slots = {tid: floor(share) for tid, share in exact.items()}
    by_remainder = sorted(exact, key=lambda tid: exact[tid] - slots[tid], reverse=True)
    for tid in by_remainder[: total_slots - sum(slots.values())]:
        slots[tid] += 1
    return slots


def plan_searches(
    session: Session,
    pref: DiscoveryPreference,
    *,
    total_slots: int = 5,
    employer_names: Sequence[str] = (),
    settings: Settings | None = None,
) -> SearchPlan:
    """Equal shares until a template has 5 runs, then the rest follows its measured score.

    Unproven is never scored as zero. CF1b stays out while ``adzuna_enabled`` is false.
    CF3 needs an employer name.
    """
    cfg = settings or get_settings()
    if total_slots < 1:
        raise ValueError("total_slots must be >= 1")
    if not pref.target_roles or not pref.locations:
        raise ValueError("discovery preference needs at least one role and location")

    runnable: list[str] = []
    for spec in TEMPLATE_SPECS:
        if not is_template_enabled(spec.id, cfg):
            continue
        if spec.needs_employer and not employer_names:
            continue
        runnable.append(spec.id)

    stats = {
        row.template_id: row
        for row in session.scalars(select(TemplateStats).where(TemplateStats.template_id.in_(runnable)))
    }
    proven = [t for t in runnable if t in stats and stats[t].run_count >= UNPROVEN_RUN_THRESHOLD]
    unproven = [t for t in runnable if t not in proven]

    weights = _weights(proven, stats)
    nothing_to_favour = not unproven and not any(weights.values())
    if not proven or nothing_to_favour:
        exploration_slots = total_slots
        slots_by_id = _distribute_equal(runnable, total_slots)
    else:
        exploration_slots = min(total_slots, max(1, ceil(total_slots * EXPLORATION_SHARE)))
        # With nothing unproven, the reserve still stops a low scorer being dropped for good.
        explored = _distribute_equal(unproven or runnable, exploration_slots)
        scored = _distribute_by_weight(weights, total_slots - exploration_slots)
        slots_by_id = {t: explored.get(t, 0) + scored.get(t, 0) for t in runnable}

    items: list[PlannedSearch] = []
    for spec in TEMPLATE_SPECS:
        slots = slots_by_id.get(spec.id, 0)
        if slots <= 0:
            continue
        employer = employer_names[0] if spec.needs_employer and employer_names else None
        first, *more = build_queries(spec.id, pref, employer_name=employer, limit=slots)
        items.append(
            PlannedSearch(
                template_id=spec.id,
                provider=resolve_provider(spec, cfg),
                query=first.text,
                location=first.location,
                site=first.site,
                request_slots=slots,
                more_queries=tuple(more),
            )
        )

    return SearchPlan(items=tuple(items), total_slots=total_slots, exploration_slots=exploration_slots)
