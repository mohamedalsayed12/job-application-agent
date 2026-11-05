"""Cold-start and unproven-aware search planning (T5, T6, D11)."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings, get_settings
from app.models import TemplateStats
from app.search.queries import BuiltQuery, build_query
from app.search.templates import (
    TEMPLATE_SPECS,
    UNPROVEN_RUN_THRESHOLD,
    is_template_enabled,
    resolve_provider,
)

EXPLORATION_SHARE = 0.20


@dataclass(frozen=True, slots=True)
class PlannedSearch:
    template_id: str
    provider: str
    query: str
    site: str | None
    request_slots: int


@dataclass(frozen=True, slots=True)
class SearchPlan:
    items: tuple[PlannedSearch, ...]
    total_slots: int
    exploration_slots: int


def _run_counts(session: Session, template_ids: Sequence[str]) -> dict[str, int]:
    counts = {tid: 0 for tid in template_ids}
    if not template_ids:
        return counts
    rows = session.scalars(
        select(TemplateStats).where(TemplateStats.template_id.in_(list(template_ids)))
    ).all()
    for row in rows:
        counts[row.template_id] = int(row.run_count)
    return counts


def _is_unproven(run_count: int) -> bool:
    return run_count < UNPROVEN_RUN_THRESHOLD


def _distribute_equal(template_ids: Sequence[str], total_slots: int) -> dict[str, int]:
    if not template_ids or total_slots <= 0:
        return {tid: 0 for tid in template_ids}
    n = len(template_ids)
    base, rem = divmod(total_slots, n)
    return {
        tid: base + (1 if i < rem else 0) for i, tid in enumerate(template_ids)
    }


def plan_cold_start(
    session: Session,
    pref: DiscoveryPreference,
    *,
    total_slots: int = 5,
    employer_names: Sequence[str] = (),
    settings: Settings | None = None,
    run_counts: Mapping[str, int] | None = None,
) -> SearchPlan:
    """Equal share across enabled runnable templates when stats are empty or unproven (T5/T6).

    Templates with ``run_count < 5`` stay unproven — never scored as zero yield.
    CF1b stays out while ``adzuna_enabled`` is false. CF3 needs an employer name.
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
        if spec.id == "CF3" and not employer_names:
            continue
        runnable.append(spec.id)

    counts = dict(run_counts) if run_counts is not None else _run_counts(session, runnable)
    for tid in runnable:
        counts.setdefault(tid, 0)

    # Cold start / all unproven: equal share (T5). Mixed: exploitation vs exploration (D11).
    proven = [tid for tid in runnable if not _is_unproven(counts[tid])]
    unproven = [tid for tid in runnable if _is_unproven(counts[tid])]

    if not proven:
        slots_by_id = _distribute_equal(runnable, total_slots)
        exploration_slots = total_slots
    else:
        exploration_slots = max(1, round(total_slots * EXPLORATION_SHARE)) if unproven else 0
        exploitation_slots = total_slots - exploration_slots
        # Unproven never get zero from "no score" — they take the exploration share (T6).
        explore_alloc = _distribute_equal(unproven, exploration_slots) if unproven else {}
        # Proven share equally until Step 11 adds value-per-£ ranking (T7).
        exploit_alloc = _distribute_equal(proven, exploitation_slots) if proven else {}
        slots_by_id = {tid: 0 for tid in runnable}
        for tid, n in explore_alloc.items():
            slots_by_id[tid] = n
        for tid, n in exploit_alloc.items():
            slots_by_id[tid] = slots_by_id.get(tid, 0) + n

    items: list[PlannedSearch] = []
    for spec in TEMPLATE_SPECS:
        slots = slots_by_id.get(spec.id, 0)
        if slots <= 0:
            continue
        employer = employer_names[0] if spec.id == "CF3" and employer_names else None
        built: BuiltQuery = build_query(spec.id, pref, employer_name=employer)
        items.append(
            PlannedSearch(
                template_id=spec.id,
                provider=resolve_provider(spec, cfg),
                query=built.text,
                site=built.site,
                request_slots=slots,
            )
        )

    return SearchPlan(
        items=tuple(items),
        total_slots=total_slots,
        exploration_slots=exploration_slots if proven else total_slots,
    )
