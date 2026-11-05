"""Lever Postings API client (global, then EU on not_found — T18)."""

from __future__ import annotations

from urllib.parse import quote

from app.boards.ids import normalize_board_id
from app.boards.parse import label_gateway_outcome, parse_lever_jobs
from app.boards.result import BoardFetchResult, BoardInstance
from app.fetch.gateway import FetchGateway, FetchRequest
from app.models import FetchOutcome

LEVER_GLOBAL_URL = "https://api.lever.co/v0/postings/{site}?mode=json"
LEVER_EU_URL = "https://api.eu.lever.co/v0/postings/{site}?mode=json"


async def fetch_lever_board(
    gateway: FetchGateway,
    site: str,
) -> BoardFetchResult:
    site_id = normalize_board_id(site)
    if site_id is None:
        return BoardFetchResult(
            label=FetchOutcome.failed,
            system="lever",
            instance=None,
            detail="invalid_board_id",
        )

    global_result = await _fetch_instance(
        gateway,
        site_id=site_id,
        instance="lever_global",
        url_template=LEVER_GLOBAL_URL,
    )
    if global_result.label != FetchOutcome.not_found:
        return global_result

    eu_result = await _fetch_instance(
        gateway,
        site_id=site_id,
        instance="lever_eu",
        url_template=LEVER_EU_URL,
    )
    attempts: tuple[BoardInstance, ...] = ("lever_global", "lever_eu")
    if eu_result.label == FetchOutcome.not_found:
        return BoardFetchResult(
            label=FetchOutcome.not_found,
            system="lever",
            instance="lever_eu",
            status_code=eu_result.status_code,
            url=eu_result.url,
            raw_body=eu_result.raw_body,
            detail=eu_result.detail,
            attempts=attempts,
        )
    return BoardFetchResult(
        label=eu_result.label,
        system="lever",
        instance=eu_result.instance,
        jobs=eu_result.jobs,
        status_code=eu_result.status_code,
        url=eu_result.url,
        raw_body=eu_result.raw_body,
        detail=eu_result.detail,
        attempts=attempts,
    )


async def _fetch_instance(
    gateway: FetchGateway,
    *,
    site_id: str,
    instance: BoardInstance,
    url_template: str,
) -> BoardFetchResult:
    url = url_template.format(site=quote(site_id, safe="-_.~"))
    outcome = await gateway.fetch(FetchRequest(url=url, kind="api"))
    return label_gateway_outcome(
        outcome,
        system="lever",
        instance=instance,
        parse_jobs=parse_lever_jobs,
    )
