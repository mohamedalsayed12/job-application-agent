"""Orchestrate code-only extraction over stored search results (Stage 3)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.extraction.persist import persist_code_hits
from app.extraction.postings import (
    extract_from_adzuna_payload,
    extract_from_reed_payload,
    extract_from_search_result_row,
)
from app.extraction.types import CodeHit
from app.models import Candidate, SearchResult


def extract_hits_from_search_results(
    results: Iterable[SearchResult],
) -> list[CodeHit]:
    hits: list[CodeHit] = []
    for row in results:
        hits.extend(
            extract_from_search_result_row(
                url=row.url,
                title=row.title,
                snippet=row.snippet,
                search_result_id=row.id,
            )
        )
    return hits


def extract_and_persist_search_results(
    session: Session,
    results: Iterable[SearchResult],
) -> list[Candidate]:
    hits = extract_hits_from_search_results(results)
    return persist_code_hits(session, hits)


def extract_and_persist_reed_payload(
    session: Session,
    payload: object,
    *,
    search_result_ids: dict[str, uuid.UUID] | None = None,
) -> list[Candidate]:
    hits = extract_from_reed_payload(payload, search_result_ids=search_result_ids)
    return persist_code_hits(session, hits)


def extract_and_persist_adzuna_payload(
    session: Session,
    payload: object,
    *,
    search_result_ids: dict[str, uuid.UUID] | None = None,
) -> list[Candidate]:
    hits = extract_from_adzuna_payload(payload, search_result_ids=search_result_ids)
    return persist_code_hits(session, hits)
