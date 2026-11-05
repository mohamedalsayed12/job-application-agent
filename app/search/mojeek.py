"""Mojeek Search API client via the fetch gateway (D29)."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from app.core.config import Settings, get_settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.search.types import ProviderFailure, ProviderOutcome, ProviderSuccess, SearchHit

MOJEEK_SEARCH_URL = "https://api.mojeek.com/search"


def _parse_hits(payload: object) -> tuple[SearchHit, ...] | ProviderFailure:
    if not isinstance(payload, dict):
        return ProviderFailure(reason="mojeek_invalid_json", retryable=False)
    response = payload.get("response")
    if not isinstance(response, dict):
        return ProviderFailure(reason="mojeek_missing_response", retryable=False)
    status = str(response.get("status", ""))
    if status.upper().startswith("ERROR"):
        return ProviderFailure(reason=f"mojeek_status:{status}", retryable=True)
    raw_results = response.get("results")
    if raw_results is None:
        return ProviderSuccess(hits=())
    if not isinstance(raw_results, list):
        return ProviderFailure(reason="mojeek_bad_results", retryable=False)
    hits: list[SearchHit] = []
    saw_row = False
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        saw_row = True
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title")
        desc = item.get("desc")
        hits.append(
            SearchHit(
                url=url.strip(),
                title=title.strip() if isinstance(title, str) else "",
                snippet=desc.strip() if isinstance(desc, str) else "",
            )
        )
    if saw_row and not hits:
        return ProviderFailure(reason="no_usable_urls", retryable=False)
    return ProviderSuccess(hits=tuple(hits))


async def search_mojeek(
    gateway: FetchGateway,
    *,
    query: str,
    site: str | None = None,
    count: int = 10,
    settings: Settings | None = None,
) -> ProviderOutcome:
    cfg = settings or get_settings()
    api_key = cfg.mojeek_api_key.strip()
    if not api_key:
        return ProviderFailure(reason="mojeek_missing_api_key", retryable=False)
    params: dict[str, str] = {
        "q": query,
        "api_key": api_key,
        "fmt": "json",
        "t": str(max(1, min(count, 50))),
        "lb": "EN",
        "lbb": "100",
        "rb": "GB",
        "rbb": "10",
    }
    if site:
        params["site"] = site
    url = f"{MOJEEK_SEARCH_URL}?{urlencode(params)}"
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    return _from_http(outcome)


def _from_http(response: FetchResponse) -> ProviderOutcome:
    if response.status_code == 429:
        return ProviderFailure(reason="mojeek_rate_limited", retryable=True)
    if response.status_code >= 500:
        return ProviderFailure(reason=f"mojeek_http_{response.status_code}", retryable=True)
    if response.status_code != 200:
        return ProviderFailure(
            reason=f"mojeek_http_{response.status_code}",
            retryable=response.status_code >= 500,
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderFailure(reason="mojeek_invalid_json", retryable=False)
    return _parse_hits(payload)
