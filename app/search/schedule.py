"""Materialise a SearchPlan into planned search_run rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import SearchRun, SearchRunStatus
from app.search.planner import SearchPlan
from app.search.queries import BuiltQuery


def create_runs_from_plan(
    session: Session,
    plan: SearchPlan,
    *,
    applicant_id: uuid.UUID,
    now: datetime | None = None,
) -> list[SearchRun]:
    """One ``planned`` search_run per different query, up to each template's slots.

    Created in one transaction, so all of a plan's runs share ``created_at``. Outcome
    credit relies on that to tell one plan's runs from a later plan's.
    """
    stamp = now or datetime.now(UTC)
    runs: list[SearchRun] = []
    for item in plan.items:
        if item.request_slots <= 0:
            continue
        first = BuiltQuery(text=item.query, location=item.location, site=item.site)
        for built in (first, *item.more_queries):
            # Encode Reed location in query when present so the runner can split it.
            query = built.text
            if built.location and item.provider == "reed":
                query = f"{built.text}\t{built.location}"
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
