"""Ask a board about one listing, to confirm it has closed before a job is closed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from app.boards.ids import normalize_board_id
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest

_GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}"
_LEVER_URLS = {
    "lever_global": "https://api.lever.co/v0/postings/{board}/{job}?mode=json",
    "lever_eu": "https://api.eu.lever.co/v0/postings/{board}/{job}?mode=json",
}


class ListingState(StrEnum):
    gone = "gone"
    listed = "listed"
    unclear = "unclear"


@dataclass(frozen=True, slots=True)
class ListingCheck:
    state: ListingState
    url: str | None = None
    status_code: int | None = None


async def check_listing(
    gateway: FetchGateway,
    *,
    system: str,
    instance: str,
    board_id: str,
    job_id: str,
) -> ListingCheck:
    """Only a 404 means gone. Errors, refusals and odd answers are unclear, and close nothing."""
    template = _GREENHOUSE_URL if system == "greenhouse" else _LEVER_URLS.get(instance)
    board = normalize_board_id(board_id)
    if template is None or board is None:
        return ListingCheck(ListingState.unclear)
    url = template.format(board=quote(board, safe="-_.~"), job=quote(job_id, safe="-_.~"))
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    if isinstance(outcome, FetchRefusal):
        return ListingCheck(ListingState.unclear, url=url)
    if outcome.status_code == 404:
        return ListingCheck(ListingState.gone, url=url, status_code=404)
    if outcome.status_code == 200 and _names_job(outcome.body, job_id):
        return ListingCheck(ListingState.listed, url=url, status_code=200)
    return ListingCheck(ListingState.unclear, url=url, status_code=outcome.status_code)


def _names_job(body: bytes, job_id: str) -> bool:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and str(payload.get("id")) == job_id
