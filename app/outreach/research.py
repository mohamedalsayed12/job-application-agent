"""Enrich a contact with public About text and posts via Apify."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import OutreachContact, OutreachResearch, SpendKind
from app.outreach.apify_run import ApifyRun
from app.outreach.normalize import normalize_research


@dataclass(slots=True)
class ResearchResult:
    research: OutreachResearch | None = None
    problem: str | None = None
    stopped_by: str | None = None


async def research_contact(
    session: Session,
    contact: OutreachContact,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> ResearchResult:
    clock = now or (lambda: datetime.now(UTC))
    existing = session.scalar(select(OutreachResearch).where(OutreachResearch.contact_id == contact.id))
    if existing is not None and (existing.about_text or existing.quotes):
        return ResearchResult(research=existing)

    # Use hooks already on the contact evidence when there is no profile URL.
    if not contact.linkedin_url:
        return _from_evidence(session, contact, existing, clock())

    token = settings.apify_api_token.strip()
    if not token:
        return ResearchResult(stopped_by="not_configured", research=existing)

    gate = BudgetGate(session, clock=clock)
    cost = estimate_cost(provider="apify", kind=SpendKind.linkedin_profile, units=Decimal(1), settings=settings)
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider="apify",
                kind=SpendKind.linkedin_profile,
                units=Decimal(1),
                unit_label="profile_run",
                estimated_cost=cost,
                related_item_id=contact.id,
            )
        )
    except BudgetWait:
        return ResearchResult(stopped_by="budget", research=existing)
    persist()

    runner = ApifyRun(gateway, token, settings, sleep=sleep or (lambda _: None))
    items, usd = await runner.run(
        settings.apify_outreach_profile_actor,
        {
            "profileUrls": [contact.linkedin_url],
            "urls": [contact.linkedin_url],
            "username": contact.linkedin_url,
            "includePosts": True,
            "maxPosts": 5,
        },
        max_charge_usd=(cost / settings.usd_to_gbp).quantize(Decimal("0.01")),
    )
    if usd is None:
        gate.settle(reservation, actual_cost=cost)
    else:
        gate.settle(reservation, actual_cost=(usd * settings.usd_to_gbp).quantize(Decimal("0.0001")))
    persist()

    if not items:
        # Fall back to HM-finder hooks already stored on the contact.
        fallback = _from_evidence(session, contact, existing, clock())
        fallback.problem = runner.problem or "profile_empty"
        return fallback

    about, quotes = normalize_research(items[0])
    body = json.dumps({"about": about, "quotes": quotes}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    at = clock()
    if existing is None:
        existing = OutreachResearch(contact_id=contact.id, about_text=about, quotes=quotes, raw_hash=digest, fetched_at=at)
        session.add(existing)
    else:
        existing.about_text = about
        existing.quotes = quotes
        existing.raw_hash = digest
        existing.fetched_at = at
    persist()
    return ResearchResult(research=existing)


def _from_evidence(
    session: Session,
    contact: OutreachContact,
    existing: OutreachResearch | None,
    at: datetime,
) -> ResearchResult:
    snippets = contact.evidence.get("snippets") if isinstance(contact.evidence, dict) else None
    quotes = [{"text": str(s)[:500], "source_url": ""} for s in (snippets or []) if str(s).strip()]
    about = quotes[0]["text"] if quotes else None
    body = json.dumps({"about": about, "quotes": quotes}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    if existing is None:
        existing = OutreachResearch(
            contact_id=contact.id,
            about_text=about,
            quotes=quotes,
            raw_hash=digest,
            fetched_at=at,
        )
        session.add(existing)
        session.flush()
    return ResearchResult(research=existing)
