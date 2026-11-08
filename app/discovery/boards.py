"""Find a company's Greenhouse or Lever board through the public board APIs.

Google does not reliably honour ``site:`` for board hosts (audit round 2), so
web search cannot find these employers. Each board system answers directly
for a slug: a guess from the company name or domain either exists or 404s.
A board is accepted only when its own name is the same company.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway, FetchRequest, FetchResponse
from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    Candidate,
    CandidateState,
    ReviewItem,
)
from app.queue.names import clean_company_name, core_name, same_company
from app.verify.html import page_title

_MAX_SLUGS = 4
_REQUEUE_FROM = (CandidateState.needs_review, CandidateState.unsupported, CandidateState.retry_wait)

BoardSystem = Literal["greenhouse", "lever"]


@dataclass(frozen=True, slots=True)
class FoundBoard:
    system: BoardSystem
    instance: str
    board_id: str
    board_name: str


def board_slugs(candidate: Candidate) -> list[str]:
    """Likely board ids: the name joined and hyphenated, then domain labels."""
    def from_name(name: str) -> list[str]:
        words = [w for w in name.replace("&", " ").split() if w.isalnum()]
        return ["".join(words), "-".join(words)] if words else []

    labels = [
        d.removeprefix("www.").removeprefix("careers.").split(".")[0]
        for d in [candidate.primary_domain, *(candidate.domain_candidates or [])]
        if d
    ]
    slugs = [
        *from_name(core_name(candidate.company_name)),
        *(label for label in labels if label.replace("-", "").isalnum()),
        *from_name(clean_company_name(candidate.company_name)),
    ]
    return list(dict.fromkeys(s.lower() for s in slugs))[:_MAX_SLUGS]


async def find_public_board(gateway: FetchGateway, candidate: Candidate) -> FoundBoard | None:
    for slug in board_slugs(candidate):
        found = await _greenhouse(gateway, slug) or await _lever(gateway, slug)
        if found is not None and same_company(found.board_name, candidate.company_name):
            return found
    return None


async def attach_public_board(
    session: Session, gateway: FetchGateway, candidate: Candidate
) -> FoundBoard | None:
    """Attach a matching board to a board-less candidate and queue it for verification.

    A classified candidate goes back to the queue because the board is new
    evidence. Its open review item is deleted: nobody decided it, and its
    question no longer applies. The verifier opens a new one if it is needed.
    """
    if candidate.board_id or candidate.state in CANDIDATE_LIVE_UNIQUE_EXCLUDES:
        return None
    found = await find_public_board(gateway, candidate)
    if found is None or _board_taken(session, found):
        return None
    candidate.board_system = found.system
    candidate.board_instance = found.instance
    candidate.board_id = found.board_id
    if candidate.state in _REQUEUE_FROM:
        candidate.state = CandidateState.queued
        candidate.next_attempt_at = datetime.now(UTC)
        candidate.revisit_at = None
        session.execute(
            delete(ReviewItem).where(
                ReviewItem.candidate_id == candidate.id,
                ReviewItem.decided_at.is_(None),
            )
        )
    session.flush()
    return found


def _board_taken(session: Session, found: FoundBoard) -> bool:
    return session.scalar(
        select(Candidate.id).where(
            Candidate.board_system == found.system,
            Candidate.board_instance == found.instance,
            Candidate.board_id == found.board_id,
            Candidate.state.not_in(CANDIDATE_LIVE_UNIQUE_EXCLUDES),
        )
    ) is not None


async def _get(gateway: FetchGateway, url: str) -> FetchResponse | None:
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    if isinstance(outcome, FetchResponse) and outcome.status_code == 200:
        return outcome
    return None


async def _greenhouse(gateway: FetchGateway, slug: str) -> FoundBoard | None:
    response = await _get(gateway, f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}")
    if response is None:
        return None
    try:
        name = json.loads(response.body).get("name")
    except (ValueError, AttributeError):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return FoundBoard("greenhouse", "greenhouse", slug, name.strip())


async def _lever(gateway: FetchGateway, slug: str) -> FoundBoard | None:
    for instance, api_host, page_host in (
        ("lever_global", "api.lever.co", "jobs.lever.co"),
        ("lever_eu", "api.eu.lever.co", "jobs.eu.lever.co"),
    ):
        if await _get(gateway, f"https://{api_host}/v0/postings/{quote(slug)}?mode=json&limit=1") is None:
            continue
        # Lever's API has no company field; the board page title carries the name.
        page = await _get(gateway, f"https://{page_host}/{quote(slug)}")
        title = page_title(page.body.decode("utf-8", errors="replace")) if page else None
        if title and title.strip().lower() != "lever":
            return FoundBoard("lever", instance, slug, title.strip())
        return None
    return None
