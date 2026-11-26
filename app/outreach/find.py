"""Find hiring managers and company figures for a job via Apify."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import Job, OutreachContact, SpendKind
from app.outreach.apify_run import ApifyRun
from app.outreach.normalize import FoundContact, linkedin_job_url, normalize_people, works_at

EMPLOYEE_TITLES = [
    "hiring manager",
    "recruiter",
    "talent acquisition",
    "head of engineering",
    "CTO",
    "CEO",
]


@dataclass(slots=True)
class FindReport:
    contacts: list[OutreachContact] = field(default_factory=list)
    added: int = 0
    problem: str | None = None
    stopped_by: str | None = None


async def find_contacts(
    session: Session,
    *,
    applicant_id: UUID,
    job: Job,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> FindReport:
    report = FindReport()
    clock = now or (lambda: datetime.now(UTC))
    token = settings.apify_api_token.strip()
    if not token:
        report.stopped_by = "not_configured"
        return report

    existing = list(
        session.scalars(
            select(OutreachContact).where(
                OutreachContact.applicant_id == applicant_id,
                OutreachContact.job_id == job.id,
            )
        )
    )
    if len(existing) >= settings.outreach_max_contacts:
        report.contacts = existing
        return report

    gate = BudgetGate(session, clock=clock)
    job_url = linkedin_job_url(job.apply_url)
    # One unit per actor we might call (HM then employees fallback).
    units = Decimal(2) if job_url else Decimal(1)
    cost = estimate_cost(provider="apify", kind=SpendKind.linkedin_people, units=units, settings=settings)
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider="apify",
                kind=SpendKind.linkedin_people,
                units=units,
                unit_label="people_run",
                estimated_cost=cost,
                related_item_id=job.id,
            )
        )
    except BudgetWait:
        report.stopped_by = "budget"
        report.contacts = existing
        return report
    persist()

    runner = ApifyRun(gateway, token, settings, sleep=sleep or (lambda _: None))
    items: list[dict[str, Any]] = []
    actor_used = ""
    per_run_charge = (settings.outreach_people_unit_price / settings.usd_to_gbp).quantize(Decimal("0.01"))
    charged_usd = Decimal(0)

    if job_url:
        actor_used = settings.apify_outreach_hm_actor
        found, usd = await runner.run(
            actor_used,
            {
                "mode": "BY_URL",
                "jobUrls": [job_url],
                "maxJobs": 1,
                "findVerifiedEmail": True,
                "generateOutreachHooks": True,
            },
            max_charge_usd=per_run_charge,
        )
        if usd is not None:
            charged_usd += usd
        if found:
            items = found
        else:
            report.problem = runner.problem or "hm_actor_empty"

    if not items:
        actor_used = settings.apify_outreach_employees_actor
        runner.problem = ""
        found, usd = await runner.run(
            actor_used,
            {
                # The actor searches LinkedIn's current-company filter; a name works where no company URL is known.
                "companies": [job.company_name],
                "jobTitles": EMPLOYEE_TITLES,
                "maxItems": settings.outreach_max_contacts,
                "profileScraperMode": "Short ($4 per 1k)",
            },
            max_charge_usd=per_run_charge,
        )
        if usd is not None:
            charged_usd += usd
        if found:
            items = found
        elif not report.problem:
            report.problem = runner.problem or "employees_actor_empty"

    gate.settle(
        reservation,
        **_actual(reservation.estimated_cost, charged_usd if charged_usd > 0 else None, settings),
    )
    persist()

    people = normalize_people(items, actor=actor_used or settings.apify_outreach_hm_actor, company_fallback=job.company_name)
    # A company searched by name also matches namesakes ("TPP Retail", another firm called TPP's staff list).
    people = [person for person in people if works_at(person, job.company_name)]
    people = people[: settings.outreach_max_contacts]
    by_url = {(c.linkedin_url or "").lower(): c for c in existing if c.linkedin_url}
    by_name = {(c.full_name or "").lower(): c for c in existing}

    for person in people:
        stored = _upsert(session, applicant_id, job, person, by_url=by_url, by_name=by_name)
        if stored is not None:
            report.added += 1
            report.contacts.append(stored)
        elif person.linkedin_url and person.linkedin_url.lower() in by_url:
            report.contacts.append(by_url[person.linkedin_url.lower()])
    # Keep contacts we already had that weren't in this run.
    seen_ids = {c.id for c in report.contacts}
    for contact in existing:
        if contact.id not in seen_ids:
            report.contacts.append(contact)
    persist()
    return report


def _upsert(
    session: Session,
    applicant_id: UUID,
    job: Job,
    person: FoundContact,
    *,
    by_url: dict[str, OutreachContact],
    by_name: dict[str, OutreachContact],
) -> OutreachContact | None:
    key = (person.linkedin_url or "").lower()
    if key and key in by_url:
        row = by_url[key]
        row.title = person.title or row.title
        row.email = person.email or row.email
        row.role_kind = person.role_kind.value
        row.evidence = person.evidence
        return None
    if not key and person.full_name.lower() in by_name:
        return None
    row = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name=person.full_name,
        title=person.title,
        company_name=person.company_name,
        linkedin_url=person.linkedin_url,
        email=person.email,
        role_kind=person.role_kind.value,
        evidence=person.evidence,
        source_actor=person.source_actor,
    )
    session.add(row)
    session.flush()
    if key:
        by_url[key] = row
    by_name[person.full_name.lower()] = row
    return row


def _actual(estimate: Decimal, cost_usd: Decimal | None, settings: Settings) -> dict[str, Any]:
    if cost_usd is None:
        return {"actual_cost": estimate}
    return {"actual_cost": (cost_usd * settings.usd_to_gbp).quantize(Decimal("0.0001"))}
