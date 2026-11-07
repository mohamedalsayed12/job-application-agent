"""Shared official-domain page fetch for Stage 5 checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse

_RETRYABLE = frozenset({"timeout", "http_error"})

PageKind = Literal["ok", "miss", "retry"]


@dataclass(frozen=True, slots=True)
class PageFetch:
    kind: PageKind
    url: str | None = None
    html: str | None = None
    reason: str | None = None


async def fetch_official_page(
    gateway: FetchGateway,
    url: str,
    *,
    domain: str,
) -> PageFetch:
    """Fetch a page under ``domain``. ``retry`` is transport failure only."""
    outcome = await gateway.fetch(
        FetchRequest(url=url, kind="page", official_domain=domain)
    )
    if isinstance(outcome, FetchRefusal):
        if outcome.reason in _RETRYABLE:
            return PageFetch(kind="retry", url=url, reason=outcome.reason)
        return PageFetch(kind="miss", url=url, reason=outcome.reason)
    assert isinstance(outcome, FetchResponse)
    if outcome.status_code != 200:
        return PageFetch(
            kind="miss",
            url=outcome.url,
            reason=f"http_{outcome.status_code}",
        )
    return PageFetch(
        kind="ok",
        url=outcome.url,
        html=outcome.body.decode("utf-8", errors="replace"),
    )
