"""Materialise a SearchPlan into planned search_run rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import SearchRun, SearchRunStatus
from app.search.planner import SearchPlan


def create_runs_from_plan(
    session: Session,
    plan: SearchPlan,
    *,
    applicant_id: uuid.UUID,
    now: datetime | None = None,
) -> list[SearchRun]:
    """One ``planned`` search_run per plan item with slots > 0.

    ``request_slots`` is planner weight for budget share; duplicate identical
    queries are not fired (page offsets land in a later step).
    """
    stamp = now or datetime.now(UTC)
    runs: list[SearchRun] = []
    for item in plan.items:
        if item.request_slots <= 0:
            continue
        # Encode Reed location in query when present so the runner can split it.
        query = item.query
        if item.location and item.provider == "reed":
            query = f"{item.query}\t{item.location}"
        run = SearchRun(
            template_id=item.template_id,
            applicant_id=applicant_id,
            query=query,
            provider=item.provider,
            status=SearchRunStatus.planned,
            attempt_count=0,
            next_attempt_at=stamp,
        )
        session.add(run)
        runs.append(run)
    session.flush()
    return runs
