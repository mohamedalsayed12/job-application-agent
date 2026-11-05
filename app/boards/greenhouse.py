"""Greenhouse Job Board API client (boards-api.greenhouse.io)."""

from __future__ import annotations

from urllib.parse import quote

from app.boards.ids import normalize_board_id
from app.boards.parse import label_gateway_outcome, parse_greenhouse_jobs
from app.boards.result import BoardFetchResult
from app.fetch.gateway import FetchGateway, FetchRequest
from app.models import FetchOutcome

GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


async def fetch_greenhouse_board(
    gateway: FetchGateway,
    board_token: str,
) -> BoardFetchResult:
    token = normalize_board_id(board_token)
    if token is None:
        return BoardFetchResult(
            label=FetchOutcome.failed,
            system="greenhouse",
            instance=None,
            detail="invalid_board_id",
        )
    url = GREENHOUSE_JOBS_URL.format(token=quote(token, safe="-_.~"))
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    return label_gateway_outcome(
        outcome,
        system="greenhouse",
        instance="greenhouse",
        parse_jobs=parse_greenhouse_jobs,
    )
