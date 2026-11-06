"""SerpAPI Google Search client via the fetch gateway (D29 default)."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from app.core.config import Settings, get_settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.search.types import (
    ProviderFailure,
    ProviderInconclusive,
    ProviderOutcome,
    ProviderSuccess,
    SearchHit,
)

SERPAPI_SEARCH_URL = "https://serpapi.com/search"


def _compose_query(query: str, site: str | None) -> str:
    """Append Google ``site:`` for BD1/BD2 domain restriction (V2)."""
    if not site:
        return query
    marker = f"site:{site}"
    if marker.lower() in query.lower():
        return query
    return f"{query} {marker}".strip()


def _parse_hits(payload: object) -> ProviderOutcome:
    if not isinstance(payload, dict):
        return ProviderFailure(reason="serpapi_invalid_json", retryable=False)
    meta = payload.get("search_metadata")
    status = ""
    if isinstance(meta, dict):
        raw_status = meta.get("status")
        if isinstance(raw_status, str):
            status = raw_status.strip().lower()
    err = payload.get("error")
    err_text = err.strip() if isinstance(err, str) else ""
    # SerpAPI returns HTTP 200 + status Success with a top-level `error` when Google
    # has zero organic hits ("Google hasn't returned any results…"). That is empty
    # success, not a provider failure.
    if status == "error" or (err_text and status != "success"):
        lower = err_text.lower() if err_text else status
        retryable = "rate" in lower or "limit" in lower or "timeout" in lower
        detail = err_text[:120] if err_text else status or "unknown"
        return ProviderFailure(reason=f"serpapi_error:{detail}", retryable=retryable)
    raw_results = payload.get("organic_results")
    if raw_results is None:
        return ProviderSuccess(hits=())
    if not isinstance(raw_results, list):
        return ProviderFailure(reason="serpapi_bad_results", retryable=False)
    hits: list[SearchHit] = []
    saw_row = False
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        saw_row = True
        url = item.get("link")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title")
        snippet = item.get("snippet")
        hits.append(
            SearchHit(
                url=url.strip(),
                title=title.strip() if isinstance(title, str) else "",
                snippet=snippet.strip() if isinstance(snippet, str) else "",
            )
        )
    if saw_row and not hits:
        return ProviderInconclusive()
    return ProviderSuccess(hits=tuple(hits))


async def search_serpapi(
    gateway: FetchGateway,
    *,
    query: str,
    site: str | None = None,
    settings: Settings | None = None,
) -> ProviderOutcome:
    cfg = settings or get_settings()
    api_key = cfg.serpapi_api_key.strip()
    if not api_key:
        return ProviderFailure(reason="serpapi_missing_api_key", retryable=False)
    params = {
        "engine": "google",
        "q": _compose_query(query, site),
        "api_key": api_key,
        "hl": "en",
        "gl": "uk",
    }
    url = f"{SERPAPI_SEARCH_URL}?{urlencode(params)}"
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    return _from_http(outcome)


def _from_http(response: FetchResponse) -> ProviderOutcome:
    if response.status_code == 429:
        return ProviderFailure(reason="serpapi_rate_limited", retryable=True)
    if response.status_code in {401, 403}:
        return ProviderFailure(
            reason=f"serpapi_http_{response.status_code}",
            retryable=False,
        )
    if response.status_code >= 500:
        return ProviderFailure(
            reason=f"serpapi_http_{response.status_code}",
            retryable=True,
        )
    if response.status_code != 200:
        return ProviderFailure(
            reason=f"serpapi_http_{response.status_code}",
            retryable=False,
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderFailure(reason="serpapi_invalid_json", retryable=False)
    return _parse_hits(payload)
