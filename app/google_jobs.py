"""Google Jobs through SerpAPI, same daily loop as LinkedIn and Indeed (D71).

Once a day each person the AI works for gets Google Jobs searches for every target role
and location, as graduate, junior and entry-level title forms (Google Jobs has no
experience filter). Each search is one SerpAPI call. Every call is reserved against the
budget first and settled at the configured search price (SerpAPI does not return a cost).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.cleaning.clean import clean_run, close_stale
from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest
from app.indeed import indeed_searches
from app.linkedin import Search
from app.models import FetchOutcome, FetchRun, RawPayload, SpendKind

SOURCE = "google_jobs"
SERPAPI_SEARCH = "https://serpapi.com/search.json"


@dataclass(slots=True)
class GoogleJobsReport:
    searches: int = 0
    jobs: int = 0
    postings_new: int = 0
    closed: int = 0
    failed: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def is_due(session: Session, *, now: datetime, settings: Settings) -> bool:
    last = session.scalar(select(func.max(FetchRun.started_at)).where(FetchRun.source == SOURCE))
    return last is None or now - last >= timedelta(hours=settings.google_jobs_every_hours)


async def collect_google_jobs(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime],
) -> GoogleJobsReport:
    report = GoogleJobsReport()
    key = settings.serpapi_api_key.strip()
    if not key:
        report.stopped_by = "not_configured"
        return report
    if not is_due(session, now=now(), settings=settings):
        return report
    gate = BudgetGate(session, clock=now)
    estimate = settings.google_jobs_max_results * settings.google_jobs_unit_price + settings.google_jobs_run_price
    for search in indeed_searches(session, settings.ai_applicants()):
        try:
            reservation = gate.reserve(
                SpendEstimate(
                    provider="serpapi",
                    kind=SpendKind.google_jobs,
                    units=Decimal(settings.google_jobs_max_results),
                    unit_label="job",
                    estimated_cost=estimate,
                )
            )
        except BudgetWait:
            report.stopped_by = "budget"
            break
        persist()
        started = now()
        items, problem = await _search(gateway, settings=settings, api_key=key, search=search)
        if items is None:
            # No billable body: do not charge the ceiling or write a FetchRun that blocks the next day.
            gate.settle(reservation, consumed=False)
            report.failed.append(f"{search.role} in {search.location}: {problem}")
            persist()
            continue
        gate.settle(reservation, actual_cost=reservation.estimated_cost)
        report.searches += 1
        stored = store_items(session, items, started_at=started, finished_at=now())
        report.jobs += len(items)
        report.postings_new += stored
        persist()
    report.closed = close_stale(session, source=SOURCE, now=now(), after=timedelta(days=settings.google_jobs_stale_days))
    persist()
    return report


async def _search(
    gateway: FetchGateway,
    *,
    settings: Settings,
    api_key: str,
    search: Search,
) -> tuple[list[dict[str, Any]] | None, str]:
    """One SerpAPI Google Jobs page. Returns (items, problem). items is None when the call failed."""
    params = {
        "engine": "google_jobs",
        "q": search.role,
        "location": search.location,
        "hl": "en",
        "gl": settings.google_jobs_gl.strip().lower() or "uk",
        "api_key": api_key,
    }
    response = await gateway.fetch(
        FetchRequest(
            url=f"{SERPAPI_SEARCH}?{urlencode(params)}",
            method="GET",
            kind="api",
            timeout_s=30.0,
        )
    )
    if isinstance(response, FetchRefusal):
        return None, response.reason
    if response.status_code >= 400:
        return None, f"SerpAPI answered {response.status_code}"
    try:
        payload = json.loads(response.body)
    except ValueError:
        return None, "SerpAPI sent something that is not JSON"
    if not isinstance(payload, dict):
        return None, "SerpAPI sent something that is not a JSON object"
    if payload.get("error"):
        return None, str(payload["error"])
    rows = payload.get("jobs_results")
    if rows is None:
        return [], ""
    if not isinstance(rows, list):
        return None, "jobs_results unreadable"
    items = [row for row in rows if isinstance(row, dict)][: settings.google_jobs_max_results]
    return items, ""


def store_items(
    session: Session, items: list[dict[str, Any]], *, started_at: datetime, finished_at: datetime, ok: bool = True
) -> int:
    """Keep one search's raw results and clean them into jobs. Returns how many postings were new."""
    rows = [item for item in items if isinstance(item, dict) and _job_id(item)]
    outcome = FetchOutcome.failed if not ok else FetchOutcome.complete if rows else FetchOutcome.empty
    run = FetchRun(
        source=SOURCE,
        outcome=outcome,
        jobs_returned=len(rows),
        request_count=1,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(run)
    session.flush()
    for item in rows:
        body = json.dumps(item, sort_keys=True, ensure_ascii=False)
        session.add(
            RawPayload(
                fetch_run_id=run.id,
                source=SOURCE,
                source_job_id=_job_id(item),
                payload=item,
                payload_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                fetched_at=finished_at,
            )
        )
    session.flush()
    return clean_run(session, run).postings_new


def _job_id(item: dict[str, Any]) -> str | None:
    value = item.get("job_id")
    if value is not None and str(value).strip():
        return str(value).strip()
    return None
