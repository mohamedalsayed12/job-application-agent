"""Reed Jobseeker API client via the fetch gateway (CF1 / D29)."""

from __future__ import annotations

import base64
import json
from urllib.parse import urlencode

from app.core.config import Settings, get_settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.search.types import ProviderFailure, ProviderOutcome, ProviderSuccess, SearchHit

REED_SEARCH_URL = "https://www.reed.co.uk/api/1.0/search"


def _basic_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _parse_hits(payload: object) -> ProviderOutcome:
    if not isinstance(payload, dict):
        return ProviderFailure(reason="reed_invalid_json", retryable=False)
    raw = payload.get("results")
    if raw is None:
        return ProviderSuccess(hits=())
    if not isinstance(raw, list):
        return ProviderFailure(reason="reed_bad_results", retryable=False)
    hits: list[SearchHit] = []
    saw_row = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        saw_row = True
        job_url = item.get("jobUrl") or item.get("externalUrl")
        if not isinstance(job_url, str) or not job_url.strip():
            continue
        title = item.get("jobTitle")
        employer = item.get("employerName")
        description = item.get("jobDescription")
        title_text = title.strip() if isinstance(title, str) else ""
        if isinstance(employer, str) and employer.strip():
            title_text = f"{title_text} — {employer.strip()}" if title_text else employer.strip()
        snippet = description.strip() if isinstance(description, str) else ""
        hits.append(SearchHit(url=job_url.strip(), title=title_text, snippet=snippet))
    if saw_row and not hits:
        return ProviderFailure(reason="no_usable_urls", retryable=False)
    return ProviderSuccess(hits=tuple(hits))


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
    url = f"{REED_SEARCH_URL}?{urlencode(params)}"
    outcome = await gateway.fetch(
        FetchRequest(
            url=url,
            kind="api",
            headers={"Authorization": _basic_auth_header(api_key)},
        )
    )
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    return _from_http(outcome)


def _from_http(response: FetchResponse) -> ProviderOutcome:
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
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderFailure(reason="reed_invalid_json", retryable=False)
    return _parse_hits(payload)
