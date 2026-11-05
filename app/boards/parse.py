"""Pure labeling of gateway outcomes into FetchOutcome (V5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.boards.result import BoardFetchResult, BoardInstance, BoardSystem
from app.fetch.gateway import FetchRefusal, FetchResponse
from app.models import FetchOutcome

JobsParser = Callable[[Any], tuple[tuple[dict[str, Any], ...], str | None]]


def label_gateway_outcome(
    outcome: FetchResponse | FetchRefusal,
    *,
    system: BoardSystem,
    instance: BoardInstance,
    parse_jobs: JobsParser,
) -> BoardFetchResult:
    if isinstance(outcome, FetchRefusal):
        return BoardFetchResult(
            label=FetchOutcome.failed,
            system=system,
            instance=instance,
            detail=outcome.reason,
            attempts=(instance,),
        )

    status = outcome.status_code
    body = outcome.body
    url = outcome.url

    if status == 404:
        return BoardFetchResult(
            label=FetchOutcome.not_found,
            system=system,
            instance=instance,
            status_code=status,
            url=url,
            raw_body=body,
            detail=_error_detail(body),
            attempts=(instance,),
        )

    if status != 200:
        return BoardFetchResult(
            label=FetchOutcome.failed,
            system=system,
            instance=instance,
            status_code=status,
            url=url,
            raw_body=body,
            detail=f"http_{status}",
            attempts=(instance,),
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return BoardFetchResult(
            label=FetchOutcome.partial,
            system=system,
            instance=instance,
            status_code=status,
            url=url,
            raw_body=body,
            detail="unparseable_json",
            attempts=(instance,),
        )

    jobs, detail = parse_jobs(payload)
    if detail is not None:
        return BoardFetchResult(
            label=FetchOutcome.partial,
            system=system,
            instance=instance,
            jobs=jobs,
            status_code=status,
            url=url,
            raw_body=body,
            detail=detail,
            attempts=(instance,),
        )

    label = FetchOutcome.empty if len(jobs) == 0 else FetchOutcome.complete
    return BoardFetchResult(
        label=label,
        system=system,
        instance=instance,
        jobs=jobs,
        status_code=status,
        url=url,
        raw_body=body,
        attempts=(instance,),
    )


def parse_greenhouse_jobs(payload: Any) -> tuple[tuple[dict[str, Any], ...], str | None]:
    if not isinstance(payload, dict) or "jobs" not in payload:
        return (), "unexpected_shape"
    raw_jobs = payload["jobs"]
    if not isinstance(raw_jobs, list) or any(not isinstance(j, dict) for j in raw_jobs):
        return (), "unexpected_shape"
    jobs = tuple(raw_jobs)
    meta = payload.get("meta")
    if isinstance(meta, dict) and "total" in meta:
        total = meta["total"]
        if not isinstance(total, int) or total != len(jobs):
            return jobs, "meta_total_mismatch"
    return jobs, None


def parse_lever_jobs(payload: Any) -> tuple[tuple[dict[str, Any], ...], str | None]:
    if not isinstance(payload, list) or any(not isinstance(j, dict) for j in payload):
        return (), "unexpected_shape"
    return tuple(payload), None


def _error_detail(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str):
            return err
    return None
