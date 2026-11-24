"""D54: LinkedIn is the one job source, read through the Black Falcon Data scraper on Apify.

Once a day each person the AI works for gets one LinkedIn search per target role and
location, for entry-level jobs posted in the last few days. Each search is one Apify run:
the gateway allows 30 seconds a request, so the run is started, then checked until it
ends, then its results are read. Every run is reserved against the budget first and
settled at what Apify says it cost.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.cleaning.clean import clean_run, close_stale
from app.core.config import Settings
from app.fetch.apify import run_actor, settle_cost
from app.fetch.due import due_searches, failed_run
from app.fetch.gateway import FetchGateway
from app.models import (
    ApplicantPreference,
    FetchOutcome,
    FetchRun,
    RawPayload,
    SpendKind,
)

SOURCE = "linkedin"
ENTRY_LEVEL = "2"  # LinkedIn f_E code for entry level; 3 is associate.


@dataclass(frozen=True, slots=True)
class Search:
    role: str
    location: str

    def url(self, *, posted_within_days: int, levels: str = ENTRY_LEVEL) -> str:
        query = {
            "keywords": self.role,
            "location": self.location,
            "f_E": ",".join(part.strip() for part in levels.split(",") if part.strip()) or ENTRY_LEVEL,
            "f_TPR": f"r{posted_within_days * 86400}",
            "sortBy": "DD",
        }
        return f"https://www.linkedin.com/jobs/search/?{urlencode(query, quote_via=quote)}"


@dataclass(slots=True)
class LinkedinReport:
    searches: int = 0
    jobs: int = 0
    postings_new: int = 0
    closed: int = 0
    failed: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def searches(session: Session, applicant_ids: set[uuid.UUID] | None) -> list[Search]:
    """Every target role in every wanted location, once each however many people share it."""
    query = select(ApplicantPreference)
    if applicant_ids is not None:
        query = query.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    found: dict[tuple[str, str], Search] = {}
    for preference in session.scalars(query):
        for role in preference.target_roles:
            for location in preference.locations or ["United Kingdom"]:
                search = Search(role.strip(), location.strip())
                found.setdefault((search.role.lower(), search.location.lower()), search)
    return sorted(found.values(), key=lambda search: (search.role.lower(), search.location.lower()))


async def collect_linkedin(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime],
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> LinkedinReport:
    report = LinkedinReport()
    token = settings.apify_api_token.strip()
    if not token:
        report.stopped_by = "not_configured"
        return report
    due = due_searches(
        session,
        SOURCE,
        searches(session, settings.ai_applicants()),
        now=now(),
        every_hours=settings.linkedin_every_hours,
        settings=settings,
    )
    if not due:
        return report
    gate = BudgetGate(session, clock=now)
    for item in due:
        search = item.search
        days = settings.search_first_posted_within_days if item.first else settings.linkedin_posted_within_days
        estimate = settings.linkedin_max_results * settings.linkedin_unit_price + settings.linkedin_run_price
        try:
            reservation = gate.reserve(
                SpendEstimate(
                    provider="apify",
                    kind=SpendKind.linkedin_jobs,
                    units=Decimal(settings.linkedin_max_results),
                    unit_label="job",
                    estimated_cost=estimate,
                )
            )
        except BudgetWait:
            report.stopped_by = "budget"
            break
        persist()
        started = now()
        max_charge = (settings.linkedin_max_results * settings.linkedin_unit_price) / settings.usd_to_gbp
        items, cost_usd, problem = await run_actor(
            gateway,
            token=token,
            actor=settings.linkedin_actor,
            body={
                "startUrls": [{"url": search.url(posted_within_days=days, levels=settings.linkedin_levels)}],
                "maxResults": settings.linkedin_max_results,
                "includeDetails": True,
                "enrichDetails": True,
                "descriptionFormat": "text",
            },
            max_charge_usd=max_charge,
            wait_minutes=settings.linkedin_wait_minutes,
            sleep=sleep,
        )
        report.searches += 1
        if items is None and cost_usd is None:
            # No Apify run id: nothing is charged, and the search is tried again after search_retry_hours.
            gate.settle(reservation, consumed=False)
            report.failed.append(f"{search.role} in {search.location}: {problem or 'did not start'}")
            failed_run(session, SOURCE, item.key, started_at=started, finished_at=now())
            persist()
            continue
        gate.settle(
            reservation,
            actual_cost=settle_cost(reservation.estimated_cost, cost_usd, settings.usd_to_gbp),
        )
        if items is None:
            report.failed.append(f"{search.role} in {search.location}: {problem}")
        stored = store_items(
            session, items or [], started_at=started, finished_at=now(), ok=items is not None, search_key=item.key
        )
        report.jobs += len(items or [])
        report.postings_new += stored
        persist()
    report.closed = close_stale(session, source=SOURCE, now=now(), after=timedelta(days=settings.linkedin_stale_days))
    persist()
    return report


def store_items(
    session: Session,
    items: list[dict[str, Any]],
    *,
    started_at: datetime,
    finished_at: datetime,
    ok: bool = True,
    search_key: str | None = None,
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
        search_key=search_key,
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
    value = item.get("linkedinJobId") or item.get("jobId")
    return str(value).strip() or None if value is not None else None
