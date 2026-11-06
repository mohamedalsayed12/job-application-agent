"""Employer names and apply links from Reed / Adzuna posting payloads."""

from __future__ import annotations

import uuid
from typing import Any

from app.extraction.types import CodeHit
from app.extraction.urls import parse_board_from_url

_TITLE_EMPLOYER_SEP = " — "


def extract_from_reed_payload(
    payload: object,
    *,
    search_result_ids: dict[str, uuid.UUID] | None = None,
) -> list[CodeHit]:
    """Parse a Reed Jobseeker search JSON body into code hits."""
    rows = _list_under(payload, "results")
    if rows is None:
        return []
    hits: list[CodeHit] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        employer = _str(item.get("employerName"))
        apply_url = _first_url(item.get("externalUrl"), item.get("jobUrl"))
        if not employer and not apply_url:
            continue
        hits.extend(
            _hits_for_posting(
                company_name=employer,
                apply_url=apply_url,
                quote_bits=(employer, apply_url),
                search_result_ids=search_result_ids,
            )
        )
    return hits


def extract_from_adzuna_payload(
    payload: object,
    *,
    search_result_ids: dict[str, uuid.UUID] | None = None,
) -> list[CodeHit]:
    """Parse an Adzuna search JSON body into code hits."""
    rows = _list_under(payload, "results")
    if rows is None:
        return []
    hits: list[CodeHit] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        company = item.get("company")
        employer = None
        if isinstance(company, dict):
            employer = _str(company.get("display_name"))
        elif isinstance(company, str):
            employer = _str(company)
        apply_url = _first_url(item.get("redirect_url"), item.get("adref"))
        if not employer and not apply_url:
            continue
        hits.extend(
            _hits_for_posting(
                company_name=employer,
                apply_url=apply_url,
                quote_bits=(employer, apply_url),
                search_result_ids=search_result_ids,
            )
        )
    return hits


def extract_from_search_result_row(
    *,
    url: str,
    title: str,
    snippet: str,
    search_result_id: uuid.UUID | None = None,
) -> list[CodeHit]:
    """Code extraction from one stored ``search_result`` evidence row."""
    board = parse_board_from_url(url)
    employer = _employer_from_reed_style_title(title)
    quote = (url or title or snippet).strip()
    if not quote:
        quote = url
    if board is None and employer is None:
        return []
    return [
        CodeHit(
            company_name=employer,
            board=board,
            quoted_text=quote[:200],
            source_url=url,
            search_result_id=search_result_id,
            apply_url=url or None,
        )
    ]


def _hits_for_posting(
    *,
    company_name: str | None,
    apply_url: str | None,
    quote_bits: tuple[str | None, ...],
    search_result_ids: dict[str, uuid.UUID] | None,
) -> list[CodeHit]:
    board = parse_board_from_url(apply_url) if apply_url else None
    if company_name is None and board is None:
        return []
    quote = " | ".join(bit for bit in quote_bits if bit) or (apply_url or company_name or "")
    result_id = None
    if apply_url and search_result_ids:
        result_id = search_result_ids.get(apply_url)
    return [
        CodeHit(
            company_name=company_name,
            board=board,
            quoted_text=quote[:200],
            source_url=apply_url or "",
            search_result_id=result_id,
            apply_url=apply_url,
        )
    ]


def _employer_from_reed_style_title(title: str) -> str | None:
    if _TITLE_EMPLOYER_SEP not in title:
        return None
    _, _, right = title.partition(_TITLE_EMPLOYER_SEP)
    return _str(right)


def _list_under(payload: object, key: str) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    return raw


def _str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_url(*values: object) -> str | None:
    for value in values:
        text = _str(value)
        if text and text.startswith(("http://", "https://")):
            return text
    return None
