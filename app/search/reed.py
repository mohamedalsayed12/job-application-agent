"""Reed Jobseeker API client via the fetch gateway (CF1 / D29)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlencode

from app.core.config import Settings, get_settings
from app.extraction.postings import format_employer_snippet
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.search.types import (
    ProviderFailure,
    ProviderInconclusive,
    ProviderOutcome,
    ProviderSuccess,
    SearchHit,
)

REED_SEARCH_URL = "https://www.reed.co.uk/api/1.0/search"
REED_DETAILS_URL = "https://www.reed.co.uk/api/1.0/jobs/{job_id}"


@dataclass(frozen=True, slots=True)
class _ReedSearchRow:
    job_id: int | None
    job_url: str | None
    title: str
    employer: str
    description: str


def _basic_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def _http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith(("http://", "https://")):
        return text
    return None


def _job_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": _basic_auth_header(api_key)}


def _hit_from_row(row: _ReedSearchRow, apply_url: str) -> SearchHit:
    return SearchHit(
        url=apply_url,
        title=row.title,
        snippet=format_employer_snippet(row.employer, row.description),
    )


def _parse_search_rows(payload: object) -> tuple[list[_ReedSearchRow], int] | ProviderFailure:
    """Return (rows, raw_result_count) or a parse failure."""
    if not isinstance(payload, dict):
        return ProviderFailure(reason="reed_invalid_json", retryable=False)
    raw = payload.get("results")
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return ProviderFailure(reason="reed_bad_results", retryable=False)
    rows: list[_ReedSearchRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("jobTitle")
        employer = item.get("employerName")
        description = item.get("jobDescription")
        rows.append(
            _ReedSearchRow(
                job_id=_job_id(item.get("jobId")),
                job_url=_http_url(item.get("jobUrl")),
                title=title.strip() if isinstance(title, str) else "",
                employer=employer.strip() if isinstance(employer, str) else "",
                description=(
                    description.strip() if isinstance(description, str) else ""
                ),
            )
        )
    return rows, len(raw)


def _status_failure(response: FetchResponse) -> ProviderFailure | None:
    if response.status_code == 429:
        return ProviderFailure(reason="reed_rate_limited", retryable=True)
    if response.status_code in {401, 403}:
        return ProviderFailure(reason=f"reed_http_{response.status_code}", retryable=False)
    if response.status_code >= 500:
        return ProviderFailure(reason=f"reed_http_{response.status_code}", retryable=True)
    if response.status_code != 200:
        return ProviderFailure(
            reason=f"reed_http_{response.status_code}",
            retryable=False,
        )
    return None


async def _details_external_url(
    gateway: FetchGateway,
    *,
    job_id: int,
    api_key: str,
) -> str | None:
    # Reed search does not return External Url; details does (Jobseeker docs).
    outcome = await gateway.fetch(
        FetchRequest(
            url=REED_DETAILS_URL.format(job_id=job_id),
            kind="api",
            headers=_auth_headers(api_key),
        )
    )
    if isinstance(outcome, FetchRefusal) or outcome.status_code != 200:
        return None
    try:
        payload = json.loads(outcome.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _http_url(payload.get("externalUrl"))


async def search_reed(
    gateway: FetchGateway,
    *,
    keywords: str,
    location_name: str | None = None,
    results_to_take: int = 20,
    settings: Settings | None = None,
) -> ProviderOutcome:
    cfg = settings or get_settings()
    api_key = cfg.reed_api_key.strip()
    if not api_key:
        return ProviderFailure(reason="reed_missing_api_key", retryable=False)
    params: dict[str, str] = {
        "keywords": keywords,
        "resultsToTake": str(max(1, min(results_to_take, 100))),
    }
    if location_name:
        params["locationName"] = location_name
    outcome = await gateway.fetch(
        FetchRequest(
            url=f"{REED_SEARCH_URL}?{urlencode(params)}",
            kind="api",
            headers=_auth_headers(api_key),
        )
    )
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    failed = _status_failure(outcome)
    if failed is not None:
        return failed
    try:
        payload = json.loads(outcome.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderFailure(reason="reed_invalid_json", retryable=False)
    parsed = _parse_search_rows(payload)
    if isinstance(parsed, ProviderFailure):
        return parsed
    rows, raw_count = parsed
    hits: list[SearchHit] = []
    for row in rows:
        apply_url = row.job_url
        if row.job_id is not None:
            external = await _details_external_url(
                gateway, job_id=row.job_id, api_key=api_key
            )
            if external is not None:
                apply_url = external
        if apply_url is None:
            continue
        hits.append(_hit_from_row(row, apply_url))
    if raw_count > 0 and not hits:
        return ProviderInconclusive()
    return ProviderSuccess(hits=tuple(hits))
