"""CF3 for one candidate: find its official domain before verification (Step 10 R1/R2/R4/R5)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings, get_settings
from app.extraction import (
    AiBudgetWait,
    AiInFlightWait,
    AiTokenCapWait,
    extract_ai_leftovers,
)
from app.fetch.gateway import FetchGateway, FetchRequest, FetchResponse
from app.models import Candidate, SearchResult, SearchRun, SearchRunStatus
from app.queue.names import clean_company_name
from app.search import SearchBudgetWait, run_search
from app.search.queries import build_query
from app.verify.blocklist import is_aggregator_domain
from app.verify.html import page_title

Cf3Status = Literal["searched", "search_failed", "budget_wait", "ai_wait"]

_LEVER_PAGE_HOSTS = {"lever_global": "jobs.lever.co", "lever_eu": "jobs.eu.lever.co"}


@dataclass(frozen=True, slots=True)
class Cf3Outcome:
    status: Cf3Status
    domains: tuple[str, ...] = ()
    detail: str = ""


async def search_official_domain(
    session: Session,
    gateway: FetchGateway,
    candidate: Candidate,
    *,
    applicant_id: uuid.UUID,
    pref: DiscoveryPreference,
    settings: Settings | None = None,
    persist: Callable[[], None] | None = None,
) -> Cf3Outcome:
    """Run CF3 for ``candidate`` and attach grounded same-name domains to it.

    Sets ``domain_searched_at`` once the search and extraction finished, which
    makes a domain-less candidate claimable (R2). Budget or AI waits leave it
    unset so the next pass tries again.
    """
    cfg = settings or get_settings()
    persist_fn = persist or session.flush

    await _adopt_lever_display_name(gateway, candidate)

    built = build_query("CF3", pref, employer_name=candidate.company_name)
    run = _earlier_search(session, built.text)
    if run is None:
        run = SearchRun(
            template_id="CF3",
            applicant_id=applicant_id,
            query=built.text,
            provider=cfg.web_search_provider,
            status=SearchRunStatus.planned,
            attempt_count=0,
        )
        session.add(run)
        persist_fn()
        searched = await run_search(session, gateway, run, settings=cfg, persist=persist)
        if isinstance(searched, SearchBudgetWait):
            return Cf3Outcome("budget_wait", detail=searched.cap)
        if searched.status == SearchRunStatus.failed:
            return Cf3Outcome("search_failed", detail=searched.error or "")

    rows = [
        row
        for row in session.scalars(select(SearchResult).where(SearchResult.search_run_id == run.id))
        if not _is_noise(row.url)
    ]
    if rows:
        extracted = await extract_ai_leftovers(
            session,
            gateway,
            rows,
            settings=cfg,
            related_run_id=run.id,
            persist=persist,
            cf3_target=candidate,
        )
        if isinstance(extracted, (AiBudgetWait, AiTokenCapWait, AiInFlightWait)):
            return Cf3Outcome("ai_wait", detail=type(extracted).__name__)

    candidate.domain_searched_at = datetime.now(UTC)
    session.flush()
    domains = tuple(
        dict.fromkeys(
            d for d in [candidate.primary_domain, *(candidate.domain_candidates or [])] if d
        )
    )
    return Cf3Outcome("searched", domains=domains)


def _earlier_search(session: Session, query: str) -> SearchRun | None:
    """A succeeded CF3 run for the same query: reuse its results, do not pay again.

    An AI wait after the search leaves the candidate unsearched, so the next
    pass reaches this point again with the results already stored.
    """
    return session.scalars(
        select(SearchRun)
        .where(
            SearchRun.template_id == "CF3",
            SearchRun.query == query,
            SearchRun.status == SearchRunStatus.succeeded,
        )
        .order_by(SearchRun.created_at.desc())
        .limit(1)
    ).first()


def _is_noise(url: str) -> bool:
    host = urlparse(url).hostname
    return host is None or is_aggregator_domain(host)


async def _adopt_lever_display_name(gateway: FetchGateway, candidate: Candidate) -> None:
    """R5: Lever has no company field, so code extraction names the row after the slug.

    The public board page title carries the company's display name.
    """
    if candidate.board_system != "lever" or not candidate.board_id:
        return
    if candidate.company_name != candidate.board_id:
        return
    host = _LEVER_PAGE_HOSTS.get(candidate.board_instance or "", "jobs.lever.co")
    fetched = await gateway.fetch(
        FetchRequest(url=f"https://{host}/{candidate.board_id}", kind="api")
    )
    if not isinstance(fetched, FetchResponse) or fetched.status_code != 200:
        return
    title = (page_title(fetched.body.decode("utf-8", errors="replace")) or "").strip()
    cleaned = clean_company_name(title)
    if not cleaned or cleaned == "lever":
        return
    candidate.company_name = title
    candidate.cleaned_name = cleaned
