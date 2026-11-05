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
        return _result(
            FetchOutcome.failed,
            system=system,
            instance=instance,
            detail=outcome.reason,
        )

    status = outcome.status_code
    body = outcome.body
    url = outcome.url

    if status == 404:
        label, jobs, detail = FetchOutcome.not_found, (), _error_detail(body)
    elif status != 200:
        label, jobs, detail = FetchOutcome.failed, (), f"http_{status}"
    else:
        label, jobs, detail = _label_ok_body(body, parse_jobs)

    return _result(
        label,
        system=system,
        instance=instance,
        jobs=jobs,
        status_code=status,
        url=url,
        raw_body=body,
        detail=detail,
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
        if type(total) is not int or total != len(jobs):
            return jobs, "meta_total_mismatch"
    return jobs, None


def parse_lever_jobs(payload: Any) -> tuple[tuple[dict[str, Any], ...], str | None]:
    if not isinstance(payload, list) or any(not isinstance(j, dict) for j in payload):
        return (), "unexpected_shape"
    return tuple(payload), None


def _label_ok_body(
    body: bytes,
    parse_jobs: JobsParser,
) -> tuple[FetchOutcome, tuple[dict[str, Any], ...], str | None]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return FetchOutcome.partial, (), "unparseable_json"
    jobs, detail = parse_jobs(payload)
    if detail is not None:
        return FetchOutcome.partial, jobs, detail
    if len(jobs) == 0:
        return FetchOutcome.empty, jobs, None
    return FetchOutcome.complete, jobs, None


def _result(
    label: FetchOutcome,
    *,
    system: BoardSystem,
    instance: BoardInstance | None,
    jobs: tuple[dict[str, Any], ...] = (),
    status_code: int | None = None,
    url: str | None = None,
    raw_body: bytes = b"",
    detail: str | None = None,
) -> BoardFetchResult:
    attempts: tuple[BoardInstance, ...] = (instance,) if instance is not None else ()
    return BoardFetchResult(
        label=label,
        system=system,
        instance=instance,
        jobs=jobs,
        status_code=status_code,
        url=url,
        raw_body=raw_body,
        detail=detail,
        attempts=attempts,
    )


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
