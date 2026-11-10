"""Select search results that code extraction could not handle."""

from __future__ import annotations

from collections.abc import Iterable

from app.extraction.ai_types import LeftoverResult
from app.extraction.postings import extract_hit
from app.extraction.types import CodeHit
from app.models import SearchResult


def partition_search_results(
    results: Iterable[SearchResult],
) -> tuple[list[CodeHit], list[LeftoverResult]]:
    """One extract_hit pass → code hits and leftovers."""
    hits: list[CodeHit] = []
    leftovers: list[LeftoverResult] = []
    for row in results:
        hit = extract_hit(
            url=row.url,
            title=row.title,
            snippet=row.snippet,
            search_result_id=row.id,
        )
        if hit is not None:
            hits.append(hit)
        else:
            leftovers.append(
                LeftoverResult(
                    id=row.id,
                    url=row.url,
                    title=row.title,
                    snippet=row.snippet,
                )
            )
    return hits, leftovers


def leftover_search_results(results: Iterable[SearchResult]) -> list[LeftoverResult]:
    return partition_search_results(results)[1]
