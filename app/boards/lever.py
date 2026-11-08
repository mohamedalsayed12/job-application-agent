"""Lever Postings API client (global, then EU on not_found; T18)."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import quote

from app.boards.ids import normalize_board_id
from app.boards.parse import label_gateway_outcome, parse_lever_jobs
from app.boards.result import BoardFetchResult, BoardInstance, invalid_board_id_result
from app.fetch.gateway import FetchGateway, FetchRequest
from app.models import FetchOutcome

LEVER_GLOBAL_URL = "https://api.lever.co/v0/postings/{site}?mode=json"
LEVER_EU_URL = "https://api.eu.lever.co/v0/postings/{site}?mode=json"
LEVER_URLS: dict[BoardInstance, str] = {"lever_global": LEVER_GLOBAL_URL, "lever_eu": LEVER_EU_URL}


async def fetch_lever_board(
    gateway: FetchGateway,
    site: str,
    *,
    instance: BoardInstance | None = None,
) -> BoardFetchResult:
    """With ``instance``, read only that host: the same name on the other host may be another company."""
    site_id = normalize_board_id(site)
    if site_id is None:
        return invalid_board_id_result("lever")

    if instance is not None:
        return await _fetch_instance(
            gateway,
            site_id=site_id,
            instance=instance,
            url_template=LEVER_URLS[instance],
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
    return replace(eu_result, attempts=("lever_global", "lever_eu"))


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
