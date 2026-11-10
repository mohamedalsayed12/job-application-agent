"""Extract board / employer hits from one stored ``search_result`` row."""

from __future__ import annotations

import uuid

from app.extraction.types import CodeHit
from app.extraction.urls import parse_board_from_url

# Written by the Reed and Google Jobs clients into snippet until search_result has a company column.
_EMPLOYER_PREFIX = "employer="


def extract_hit(
    *,
    url: str,
    title: str,
    snippet: str,
    search_result_id: uuid.UUID | None = None,
) -> CodeHit | None:
    board = parse_board_from_url(url)
    employer = _employer_from_snippet(snippet)
    if board is None and employer is None:
        return None
    quote = (url or title or snippet).strip() or url
    return CodeHit(
        company_name=employer,
        board=board,
        quoted_text=quote[:200],
        source_url=url,
        search_result_id=search_result_id,
    )


def format_employer_snippet(employer: str, description: str = "") -> str:
    """Store employer on the evidence row without inventing it from web titles."""
    name = employer.strip()
    desc = description.strip()
    if name and desc:
        return f"{_EMPLOYER_PREFIX}{name}\n{desc}"
    if name:
        return f"{_EMPLOYER_PREFIX}{name}"
    return desc


def _employer_from_snippet(snippet: str) -> str | None:
    # Writer always puts employer= on line 1; ignore later description lines.
    first = snippet.splitlines()[0] if snippet else ""
    if not first.startswith(_EMPLOYER_PREFIX):
        return None
    value = first[len(_EMPLOYER_PREFIX) :].strip()
    return value or None
