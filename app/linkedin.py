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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate, SpendReservation
from app.cleaning.clean import clean_run, close_stale
from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest
from app.models import (
    ApplicantPreference,
    FetchOutcome,
    FetchRun,
    RawPayload,
    SpendKind,
)

SOURCE = "linkedin"
APIFY_API = "https://api.apify.com/v2"
WAIT_SECONDS = 25
ENTRY_LEVEL = "2"
_FINISHED = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
_HAS_RESULTS = frozenset({"SUCCEEDED", "TIMED-OUT"})


@dataclass(frozen=True, slots=True)
class Search:
    role: str
    location: str

    def url(self, *, posted_within_days: int) -> str:
        query = {
            "keywords": self.role,
            "location": self.location,
            "f_E": ENTRY_LEVEL,
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


def is_due(session: Session, *, now: datetime, settings: Settings) -> bool:
    last = session.scalar(select(func.max(FetchRun.started_at)).where(FetchRun.source == SOURCE))
    return last is None or now - last >= timedelta(hours=settings.linkedin_every_hours)


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
    if not is_due(session, now=now(), settings=settings):
        return report
    gate = BudgetGate(session, clock=now)
    for search in searches(session, settings.ai_applicants()):
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
        run = _Run(gateway, token, settings, sleep)
        items, cost_usd = await run.search(search)
        gate.settle(reservation, **_actual(reservation, cost_usd, settings))
        report.searches += 1
        if items is None:
            report.failed.append(f"{search.role} in {search.location}: {run.problem}")
        stored = store_items(session, items or [], started_at=started, finished_at=now(), ok=items is not None)
        report.jobs += len(items or [])
        report.postings_new += stored
        persist()
    report.closed = close_stale(session, source=SOURCE, now=now(), after=timedelta(days=settings.linkedin_stale_days))
    persist()
    return report


def _actual(reservation: SpendReservation, cost_usd: Decimal | None, settings: Settings) -> dict[str, Any]:
    if cost_usd is None:
        return {"actual_cost": reservation.estimated_cost}
    return {"actual_cost": (cost_usd * settings.usd_to_gbp).quantize(Decimal("0.0001"))}


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
    value = item.get("linkedinJobId") or item.get("jobId")
    return str(value).strip() or None if value is not None else None


class _Run:
    """One Apify run of the scraper: start it, wait for it to end, read what it found."""

    def __init__(self, gateway: FetchGateway, token: str, settings: Settings, sleep: Callable[[float], Any]) -> None:
        self.gateway, self.settings, self.sleep = gateway, settings, sleep
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.problem = ""

    async def search(self, search: Search) -> tuple[list[dict[str, Any]] | None, Decimal | None]:
        settings = self.settings
        max_charge = (settings.linkedin_max_results * settings.linkedin_unit_price) / settings.usd_to_gbp
        actor = quote(settings.linkedin_actor.replace("/", "~"), safe="~")
        started = await self._call(
            f"{APIFY_API}/acts/{actor}/runs?{urlencode({'maxTotalChargeUsd': f'{max_charge:.2f}', 'memory': 1024})}",
            method="POST",
            body={
                "startUrls": [{"url": search.url(posted_within_days=settings.linkedin_posted_within_days)}],
                "maxResults": settings.linkedin_max_results,
                "includeDetails": True,
                "enrichDetails": True,
                "descriptionFormat": "text",
            },
        )
        run = (started or {}).get("data") or {}
        if not run.get("id"):
            return None, None
        deadline = settings.linkedin_wait_minutes * 60
        waited = 0
        while run.get("status") not in _FINISHED and waited < deadline:
            checked = await self._call(f"{APIFY_API}/actor-runs/{run['id']}?waitForFinish={WAIT_SECONDS}")
            if checked is None:
                await self.sleep(WAIT_SECONDS)
            run = (checked or {}).get("data") or run
            waited += WAIT_SECONDS
        if run.get("status") not in _FINISHED:
            await self._call(f"{APIFY_API}/actor-runs/{run['id']}/abort", method="POST")
            self.problem = "did not finish in time"
        cost = _usd(run.get("usageTotalUsd"))
        if run.get("status") not in _HAS_RESULTS:
            self.problem = self.problem or f"run {str(run.get('status')).lower()}"
            return None, cost
        dataset = run.get("defaultDatasetId")
        items = await self._call(f"{APIFY_API}/datasets/{dataset}/items?{urlencode({'clean': 'true', 'format': 'json'})}")
        if not isinstance(items, list):
            self.problem = self.problem or "results unreadable"
            return None, cost
        return items, cost

    async def _call(self, url: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        response = await self.gateway.fetch(
            FetchRequest(
                url=url,
                method=method,
                kind="api",
                headers=self.headers,
                timeout_s=WAIT_SECONDS + 5,
                body=json.dumps(body).encode("utf-8") if body is not None else None,
            )
        )
        if isinstance(response, FetchRefusal):
            self.problem = response.reason
            return None
        if response.status_code >= 400:
            self.problem = f"Apify answered {response.status_code}"
            return None
        try:
            return json.loads(response.body)
        except ValueError:
            self.problem = "Apify sent something that is not JSON"
            return None


def _usd(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None
