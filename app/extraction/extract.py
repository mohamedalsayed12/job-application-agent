"""Orchestrate code-only extraction over stored search results (Stage 3)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.extraction.persist import persist_code_hits
from app.extraction.postings import extract_hit
from app.extraction.types import CodeHit
from app.models import Candidate, SearchResult


def extract_hits_from_search_results(
    results: Iterable[SearchResult],
) -> list[CodeHit]:
    hits: list[CodeHit] = []
    for row in results:
        hit = extract_hit(
            url=row.url,
            title=row.title,
            snippet=row.snippet,
            search_result_id=row.id,
        )
        if hit is not None:
            hits.append(hit)
    return hits


def extract_and_persist_search_results(
    session: Session,
    results: Iterable[SearchResult],
) -> list[Candidate]:
    return persist_code_hits(session, extract_hits_from_search_results(results))
