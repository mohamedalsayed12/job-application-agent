"""D54: LinkedIn is the one job source, read through the Black Falcon Data scraper on Apify.

Once a day each person the AI works for gets one LinkedIn search per title and location: their target
roles, then the titles related to them, up to `linkedin_titles_per_person`. The searches are for jobs
at the levels they chose (D63) posted in the last few days. Each search is one Apify run:
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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.levels import (
    Levels,
    chosen_or_inferred,
    linkedin_levels,
    saved_levels,
)
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.cleaning.clean import clean_run, close_stale
from app.core.config import Settings
from app.fetch.apify import run_actor, settle_cost
from app.fetch.gateway import FetchGateway
from app.matching.related_titles import expand_titles, with_related
from app.models import (
    ApplicantPreference,
    FetchOutcome,
    FetchRun,
    RawPayload,
    SpendKind,
)

SOURCE = "linkedin"


@dataclass(frozen=True, slots=True)
class Search:
    role: str
    location: str
    levels: Levels

    def url(self, *, posted_within_days: int) -> str:
        query = {
            "keywords": self.role,
            "location": self.location,
            "f_E": linkedin_levels(self.levels),
            "f_TPR": f"r{posted_within_days * 86400}",
            "sortBy": "DD",
        }
        return f"https://www.linkedin.com/jobs/search/?{urlencode(query, quote_via=quote)}"

    def key(self) -> str:
        return f"{self.role.strip().lower()}|{self.location.strip().lower()}|{linkedin_levels(self.levels)}"


@dataclass(slots=True)
class LinkedinReport:
    searches: int = 0
    jobs: int = 0
    postings_new: int = 0
    closed: int = 0
    failed: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def searches(session: Session, applicant_ids: set[uuid.UUID] | None, *, per_person: int | None) -> list[Search]:
    """Each person's titles in every wanted location at the person's levels (D63).

    With `per_person`, the titles are their target roles and then the related titles, up to that many.
    Without it, only their target roles.

    People share a search when LinkedIn would be asked the same thing; it then carries all of their levels.
    """
    query = select(ApplicantPreference)
    if applicant_ids is not None:
        query = query.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    saved = saved_levels(session)
    found: dict[tuple[str, str, str], Search] = {}
    for preference in session.scalars(query):
        levels = chosen_or_inferred(saved.get(preference.applicant_id, frozenset()), preference.target_roles)
        titles = with_related(session, preference.target_roles)[:per_person] if per_person else preference.target_roles
        for role in titles:
            for location in preference.locations or ["United Kingdom"]:
                key = (role.strip().lower(), location.strip().lower(), linkedin_levels(levels))
                earlier = found.get(key)
                found[key] = (
                    replace(earlier, levels=earlier.levels | levels) if earlier else Search(role.strip(), location.strip(), levels)
                )
    return [found[key] for key in sorted(found)]


def done_since(session: Session, *, source: str, since: datetime) -> set[str] | None:
    """Keys of the searches this source ran since `since`, failed ones included (they retry next cycle).

    None when a run from before keys were recorded falls in that time: it stands for the whole cycle.
    """
    keys = session.scalars(select(FetchRun.search_key).where(FetchRun.source == source, FetchRun.started_at > since)).all()
    if any(key is None for key in keys):
        return None
    return set(keys)


def still_due(planned: list[Search], done: set[str] | None) -> list[Search]:
    """The planned searches not yet run this cycle, in their planned order.

    A cycle the budget cut short resumes with the searches it did not reach, the same day,
    once there is budget again.
    """
    if done is None:
        return []
    return [search for search in planned if search.key() not in done]


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
    since = now() - timedelta(hours=settings.linkedin_every_hours)
    done = done_since(session, source=SOURCE, since=since)
    people = settings.ai_applicants()
    if not still_due(searches(session, people, per_person=settings.linkedin_titles_per_person), done):
        return report
    roles = select(ApplicantPreference.target_roles)
    if people is not None:
        roles = roles.where(ApplicantPreference.applicant_id.in_(people))
    wanted = [role for row in session.scalars(roles) for role in row]
    await expand_titles(session, wanted, gateway=gateway, settings=settings, persist=persist, now=now())
    gate = BudgetGate(session, clock=now)
    planned = searches(session, people, per_person=settings.linkedin_titles_per_person)
    for search in still_due(planned, done):
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
                "startUrls": [{"url": search.url(posted_within_days=settings.linkedin_posted_within_days)}],
                "maxResults": settings.linkedin_max_results,
                "includeDetails": True,
                "enrichDetails": True,
                "descriptionFormat": "text",
            },
            max_charge_usd=max_charge,
            wait_minutes=settings.linkedin_wait_minutes,
            sleep=sleep,
        )
        gate.settle(
            reservation,
            actual_cost=settle_cost(reservation.estimated_cost, cost_usd, settings.usd_to_gbp),
        )
        report.searches += 1
        if items is None:
            report.failed.append(f"{search.role} in {search.location}: {problem}")
        stored = store_items(
            session, items or [], started_at=started, finished_at=now(), ok=items is not None, search_key=search.key()
        )
        report.jobs += len(items or [])
        report.postings_new += stored
        persist()
    if report.stopped_by is None and cycle_clean(session, source=SOURCE, since=since, planned=planned):
        report.closed = close_stale(session, source=SOURCE, now=now(), after=timedelta(days=settings.linkedin_stale_days))
        persist()
    return report


def cycle_clean(session: Session, *, source: str, since: datetime, planned: list[Search]) -> bool:
    """Whether every planned search ran since `since` without failing (D8, T4).

    Only then may jobs close: a job missing from failed, unrun or budget-stopped searches says
    nothing about whether it closed.
    """
    succeeded = set(
        session.scalars(
            select(FetchRun.search_key).where(
                FetchRun.source == source, FetchRun.started_at > since, FetchRun.outcome != FetchOutcome.failed
            )
        )
    )
    return bool(planned) and all(search.key() in succeeded for search in planned)


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
