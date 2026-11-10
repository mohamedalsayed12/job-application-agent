"""Orchestrate code-only extraction over stored search results (Stage 3)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.extraction.leftovers import partition_search_results
from app.extraction.persist import persist_code_hits
from app.models import Candidate, SearchResult


def extract_and_persist_search_results(
    session: Session,
    results: Iterable[SearchResult],
) -> list[Candidate]:
    hits, _leftovers = partition_search_results(results)
    return persist_code_hits(session, hits)
