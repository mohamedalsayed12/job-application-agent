"""SerpAPI client via the fetch gateway: Google web search (D29) and Google Jobs (D49)."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlencode, urlparse

from app.core.config import Settings, get_settings
from app.extraction.postings import format_employer_snippet
from app.extraction.urls import parse_board_from_url
from app.fetch.gateway import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    FetchGateway,
    FetchRefusal,
    FetchRequest,
    FetchResponse,
)
from app.fetch.policy import is_blocked_host
from app.search.types import (
    ProviderFailure,
    ProviderInconclusive,
    ProviderOutcome,
    ProviderSuccess,
    SearchHit,
)

SERPAPI_SEARCH_URL = "https://serpapi.com/search"
JOBS_TIMEOUT_S = MAX_TIMEOUT_S


def _compose_query(query: str, site: str | None) -> str:
    """Append Google ``site:`` for BD1/BD2 domain restriction (V2)."""
    if not site:
        return query
    marker = f"site:{site}"
    if marker.lower() in query.lower():
        return query
    return f"{query} {marker}".strip()


def _error(payload: dict) -> ProviderFailure | None:
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
    return None


def _parse_hits(payload: object) -> ProviderOutcome:
    if not isinstance(payload, dict):
        return ProviderFailure(reason="serpapi_invalid_json", retryable=False)
    if failure := _error(payload):
        return failure
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


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _job_link(item: dict) -> str:
    """The employer's own board when Google lists it, then any site we may visit, then Google's page."""
    options = item.get("apply_options")
    links = [_text(option.get("link")) for option in options if isinstance(option, dict)] if isinstance(options, list) else []
    links = [link for link in links if link]
    for link in links:
        if parse_board_from_url(link) is not None:
            return link
    for link in links:
        if not is_blocked_host((urlparse(link).hostname or "").lower()):
            return link
    return _text(item.get("share_link"))


def _parse_jobs(payload: object) -> ProviderOutcome:
    """Google Jobs rows: the employer's name always, and a board link when Google has one."""
    if not isinstance(payload, dict):
        return ProviderFailure(reason="serpapi_invalid_json", retryable=False)
    if failure := _error(payload):
        return failure
    raw_results = payload.get("jobs_results")
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
        employer, url = _text(item.get("company_name")), _job_link(item)
        if not employer or not url:
            continue
        hits.append(
            SearchHit(
                url=url,
                title=_text(item.get("title")),
                snippet=format_employer_snippet(employer, _text(item.get("description"))[:500]),
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
    return await _search(gateway, {"engine": "google", "q": _compose_query(query, site)}, _parse_hits, settings)


async def search_serpapi_jobs(
    gateway: FetchGateway,
    *,
    query: str,
    settings: Settings | None = None,
) -> ProviderOutcome:
    """Google Jobs (CF1b, D49). The place goes in the query: SerpAPI's location field wants its own names."""
    return await _search(gateway, {"engine": "google_jobs", "q": query}, _parse_jobs, settings, timeout_s=JOBS_TIMEOUT_S)


async def _search(
    gateway: FetchGateway,
    params: dict[str, str],
    parse: Callable[[object], ProviderOutcome],
    settings: Settings | None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ProviderOutcome:
    cfg = settings or get_settings()
    api_key = cfg.serpapi_api_key.strip()
    if not api_key:
        return ProviderFailure(reason="serpapi_missing_api_key", retryable=False)
    url = f"{SERPAPI_SEARCH_URL}?{urlencode({**params, 'api_key': api_key, 'hl': 'en', 'gl': 'uk'})}"
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api", timeout_s=timeout_s))
    if isinstance(outcome, FetchRefusal):
        return ProviderFailure(reason=f"fetch:{outcome.reason}", retryable=True)
    return _from_http(outcome, parse)


def _from_http(response: FetchResponse, parse: Callable[[object], ProviderOutcome]) -> ProviderOutcome:
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
    return parse(payload)
