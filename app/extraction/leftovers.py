"""Select search results that code extraction could not handle."""

from __future__ import annotations

from collections.abc import Iterable

from app.extraction.ai_types import LeftoverResult
from app.extraction.postings import extract_hit
from app.models import SearchResult


def leftover_search_results(results: Iterable[SearchResult]) -> list[LeftoverResult]:
    leftovers: list[LeftoverResult] = []
    for row in results:
        hit = extract_hit(
            url=row.url,
            title=row.title,
            snippet=row.snippet,
            search_result_id=row.id,
        )
        if hit is not None:
            continue
        leftovers.append(
            LeftoverResult(
                id=row.id,
                url=row.url,
                title=row.title,
                snippet=row.snippet,
            )
        )
    return leftovers
