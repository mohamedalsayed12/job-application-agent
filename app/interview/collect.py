"""Collect public interview stories for a role via Apify (Reddit, YouTube, LinkedIn)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.core.config import Settings
from app.fetch.apify import run_actor, settle_cost
from app.fetch.gateway import FetchGateway
from app.interview.normalize import NormalizedItem, normalize_item, queries_for
from app.models import (
    FetchOutcome,
    FetchRun,
    InterviewEvidence,
    InterviewOutcome,
    InterviewPlatform,
    RawPayload,
    SpendKind,
)

PLATFORMS = (
    InterviewPlatform.reddit,
    InterviewPlatform.youtube,
    InterviewPlatform.linkedin,
)


@dataclass(slots=True)
class CollectReport:
    platforms: int = 0
    items: int = 0
    evidence_new: int = 0
    failed: list[str] = field(default_factory=list)
    stopped_by: str | None = None
    skipped: bool = False


def evidence_count(session: Session, role_key: str) -> int:
    return int(
        session.scalar(select(func.count()).select_from(InterviewEvidence).where(InterviewEvidence.role_key == role_key))
        or 0
    )


async def collect_for_role(
    session: Session,
    *,
    role: str,
    role_key: str,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime],
    sleep: Callable[[float], Any] | None = None,
    queries: list[str] | None = None,
) -> CollectReport:
    report = CollectReport()
    if evidence_count(session, role_key) > 0:
        report.skipped = True
        return report
    token = settings.apify_api_token.strip()
    if not token:
        report.stopped_by = "not_configured"
        return report
    gate = BudgetGate(session, clock=now)
    for platform in PLATFORMS:
        estimate = (
            settings.interview_max_per_platform * settings.interview_collect_unit_price
            + settings.interview_collect_run_price
        )
        try:
            reservation = gate.reserve(
                SpendEstimate(
                    provider="apify",
                    kind=SpendKind.interview_collect,
                    units=Decimal(settings.interview_max_per_platform),
                    unit_label="item",
                    estimated_cost=estimate,
                )
            )
        except BudgetWait:
            report.stopped_by = "budget"
            break
        persist()
        started = now()
        max_charge = (
            settings.interview_max_per_platform * settings.interview_collect_unit_price
        ) / settings.usd_to_gbp
        run_kwargs: dict[str, Any] = {
            "token": token,
            "actor": actor_id(platform, settings),
            "body": actor_input(platform, role, max_items=settings.interview_max_per_platform, queries=queries),
            "max_charge_usd": max_charge,
            "wait_minutes": settings.interview_wait_minutes,
        }
        if sleep is not None:
            run_kwargs["sleep"] = sleep
        items, cost_usd, problem = await run_actor(gateway, **run_kwargs)
        gate.settle(
            reservation,
            actual_cost=settle_cost(reservation.estimated_cost, cost_usd, settings.usd_to_gbp),
        )
        report.platforms += 1
        if items is None:
            report.failed.append(f"{platform.value}: {problem}")
            persist()
            continue
        report.items += len(items)
        report.evidence_new += store_items(
            session,
            platform,
            items,
            role_key=role_key,
            started_at=started,
            finished_at=now(),
            max_snippet=settings.interview_snippet_chars,
            ok=True,
        )
        persist()
    return report


def store_items(
    session: Session,
    platform: InterviewPlatform,
    items: list[dict[str, Any]],
    *,
    role_key: str,
    started_at: datetime,
    finished_at: datetime,
    max_snippet: int,
    ok: bool = True,
) -> int:
    """Persist one platform run and its evidence rows. Returns how many evidence rows were new."""
    normalized: list[NormalizedItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = normalize_item(platform, item, max_snippet=max_snippet)
        if row is not None:
            normalized.append(row)
    outcome = FetchOutcome.failed if not ok else FetchOutcome.complete if normalized else FetchOutcome.empty
    source = f"interview_{platform.value}"
    run = FetchRun(
        source=source,
        outcome=outcome,
        jobs_returned=len(normalized),
        request_count=1,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(run)
    session.flush()
    created = 0
    for item in normalized:
        body = json.dumps(
            {"url": item.url, "title": item.title, "snippet": item.snippet},
            sort_keys=True,
            ensure_ascii=False,
        )
        session.add(
            RawPayload(
                fetch_run_id=run.id,
                source=source,
                source_job_id=item.source_id[:200],
                payload={"url": item.url, "title": item.title, "snippet": item.snippet, "platform": platform.value},
                payload_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                fetched_at=finished_at,
            )
        )
        exists = session.scalar(
            select(InterviewEvidence.id).where(
                InterviewEvidence.role_key == role_key,
                InterviewEvidence.content_hash == item.content_hash,
            )
        )
        if exists is not None:
            continue
        session.add(
            InterviewEvidence(
                role_key=role_key,
                platform=platform.value,
                url=item.url,
                title=item.title,
                snippet=item.snippet,
                outcome=InterviewOutcome.unclear.value,
                labelled=False,
                content_hash=item.content_hash,
                fetch_run_id=run.id,
            )
        )
        created += 1
    session.flush()
    return created


def actor_input(platform: InterviewPlatform, role: str, *, max_items: int, queries: list[str] | None = None) -> dict[str, Any]:
    queries = queries if queries is not None else queries_for(role)
    if platform is InterviewPlatform.reddit:
        return {
            "searches": queries,
            "maxItems": max_items,
            "maxPostCount": max_items,
            "skipComments": False,
            "searchPosts": True,
            "searchComments": True,
        }
    if platform is InterviewPlatform.youtube:
        return {"searchQueries": queries, "maxResults": max_items, "maxResultsShorts": 0}
    return {"searchQueries": queries, "searchQuery": queries[0], "limit": max_items, "maxPosts": max_items}


def actor_id(platform: InterviewPlatform, settings: Settings) -> str:
    if platform is InterviewPlatform.reddit:
        return settings.interview_reddit_actor
    if platform is InterviewPlatform.youtube:
        return settings.interview_youtube_actor
    return settings.interview_linkedin_actor
