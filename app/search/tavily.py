"""Tavily Search API client via the fetch gateway (D29 free-first default)."""

from __future__ import annotations

import json

from app.core.config import Settings, get_settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.search.types import (
    ProviderFailure,
    ProviderInconclusive,
    ProviderOutcome,
    ProviderSuccess,
    SearchHit,
)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def _parse_hits(payload: object) -> ProviderOutcome:
    if not isinstance(payload, dict):
        return ProviderFailure(reason="tavily_invalid_json", retryable=False)
    raw_results = payload.get("results")
    if raw_results is None:
        return ProviderSuccess(hits=())
    if not isinstance(raw_results, list):
        return ProviderFailure(reason="tavily_bad_results", retryable=False)
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
        content = item.get("content")
        hits.append(
            SearchHit(
                url=url.strip(),
                title=title.strip() if isinstance(title, str) else "",
                snippet=content.strip() if isinstance(content, str) else "",
            )
        )
    if saw_row and not hits:
        return ProviderInconclusive()
    return ProviderSuccess(hits=tuple(hits))


async def search_tavily(
    gateway: FetchGateway,
    *,
    query: str,
    site: str | None = None,
    count: int = 10,
    settings: Settings | None = None,
) -> ProviderOutcome:
    cfg = settings or get_settings()
    api_key = cfg.tavily_api_key.strip()
    if not api_key:
        return ProviderFailure(reason="tavily_missing_api_key", retryable=False)
    body: dict[str, object] = {
        "query": query,
        "search_depth": "basic",
        "max_results": max(1, min(count, 20)),
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if site:
        body["include_domains"] = [site]
        body["include_domains_mode"] = "restrict"
    outcome = await gateway.fetch(
        FetchRequest(
            url=TAVILY_SEARCH_URL,
            method="POST",
            kind="api",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(body).encode("utf-8"),
        )
    )
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    return _from_http(outcome)


def _from_http(response: FetchResponse) -> ProviderOutcome:
    if response.status_code == 429:
        return ProviderFailure(reason="tavily_rate_limited", retryable=True)
    if response.status_code in {401, 403, 432, 433}:
        return ProviderFailure(
            reason=f"tavily_http_{response.status_code}",
            retryable=False,
        )
    if response.status_code == 434 or response.status_code >= 500:
        return ProviderFailure(
            reason=f"tavily_http_{response.status_code}",
            retryable=True,
        )
    if response.status_code != 200:
        return ProviderFailure(
            reason=f"tavily_http_{response.status_code}",
            retryable=False,
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderFailure(reason="tavily_invalid_json", retryable=False)
    return _parse_hits(payload)
