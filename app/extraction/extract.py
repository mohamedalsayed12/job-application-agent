"""Orchestrate code-only extraction over stored search results (Stage 3)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.extraction.leftovers import partition_search_results
from app.extraction.persist import persist_code_hits
from app.extraction.types import CodeHit
from app.models import Candidate, SearchResult


def extract_hits_from_search_results(
    results: Iterable[SearchResult],
) -> list[CodeHit]:
    return partition_search_results(results)[0]


def extract_and_persist_search_results(
    session: Session,
    results: Iterable[SearchResult],
) -> list[Candidate]:
    return persist_code_hits(session, extract_hits_from_search_results(results))
