"""Run one Apify actor through the fetch gateway: start, wait, read the dataset."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest

APIFY_API = "https://api.apify.com/v2"
WAIT_SECONDS = 25
_FINISHED = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
_HAS_RESULTS = frozenset({"SUCCEEDED", "TIMED-OUT"})


async def run_actor(
    gateway: FetchGateway,
    *,
    token: str,
    actor: str,
    body: dict[str, Any],
    max_charge_usd: Decimal,
    wait_minutes: int,
    sleep: Callable[[float], Any] = asyncio.sleep,
    memory_mb: int = 1024,
) -> tuple[list[dict[str, Any]] | None, Decimal | None, str]:
    """Return (items, usage_usd, problem). ``items`` is None when the run failed."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    problem = ""
    actor_path = quote(actor.replace("/", "~"), safe="~")
    started = await _call(
        gateway,
        f"{APIFY_API}/acts/{actor_path}/runs?{urlencode({'maxTotalChargeUsd': f'{max_charge_usd:.2f}', 'memory': memory_mb})}",
        headers=headers,
        method="POST",
        body=body,
    )
    if started is None:
        return None, None, "Apify start failed"
    if isinstance(started, str):
        return None, None, started
    run = (started or {}).get("data") or {}
    if not run.get("id"):
        return None, None, "did not start"
    deadline = wait_minutes * 60
    waited = 0
    while run.get("status") not in _FINISHED and waited < deadline:
        checked = await _call(
            gateway,
            f"{APIFY_API}/actor-runs/{run['id']}?waitForFinish={WAIT_SECONDS}",
            headers=headers,
        )
        if checked is None:
            await sleep(WAIT_SECONDS)
        elif isinstance(checked, str):
            problem = checked
            await sleep(WAIT_SECONDS)
        else:
            run = (checked or {}).get("data") or run
        waited += WAIT_SECONDS
    if run.get("status") not in _FINISHED:
        await _call(gateway, f"{APIFY_API}/actor-runs/{run['id']}/abort", headers=headers, method="POST")
        problem = problem or "did not finish in time"
    cost = _usd(run.get("usageTotalUsd"))
    if run.get("status") not in _HAS_RESULTS:
        return None, cost, problem or f"run {str(run.get('status')).lower()}"
    dataset = run.get("defaultDatasetId")
    items = await _call(
        gateway,
        f"{APIFY_API}/datasets/{dataset}/items?{urlencode({'clean': 'true', 'format': 'json'})}",
        headers=headers,
    )
    if isinstance(items, str):
        return None, cost, items
    if not isinstance(items, list):
        return None, cost, "results unreadable"
    return [item for item in items if isinstance(item, dict)], cost, problem


async def _call(
    gateway: FetchGateway,
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> Any:
    response = await gateway.fetch(
        FetchRequest(
            url=url,
            method=method,
            kind="api",
            headers=headers,
            timeout_s=WAIT_SECONDS + 5,
            body=json.dumps(body).encode("utf-8") if body is not None else None,
        )
    )
    if isinstance(response, FetchRefusal):
        return response.reason
    if response.status_code >= 400:
        return f"Apify answered {response.status_code}"
    try:
        return json.loads(response.body)
    except ValueError:
        return "Apify sent something that is not JSON"


def _usd(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None


def settle_cost(estimated: Decimal, cost_usd: Decimal | None, usd_to_gbp: Decimal) -> Decimal:
    if cost_usd is None:
        return estimated
    return (cost_usd * usd_to_gbp).quantize(Decimal("0.0001"))
