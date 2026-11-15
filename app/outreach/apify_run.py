"""Start an Apify actor, wait for it, return dataset items. Shared by people and profile runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest

APIFY_API = "https://api.apify.com/v2"
WAIT_SECONDS = 25
_FINISHED = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
_HAS_RESULTS = frozenset({"SUCCEEDED", "TIMED-OUT"})


class ApifyRun:
    def __init__(
        self,
        gateway: FetchGateway,
        token: str,
        settings: Settings,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.gateway, self.settings, self.sleep = gateway, settings, sleep
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.problem = ""

    async def run(self, actor: str, body: dict[str, Any], *, max_charge_usd: Decimal) -> tuple[list[dict[str, Any]] | None, Decimal | None]:
        actor_path = quote(actor.replace("/", "~"), safe="~")
        started = await self._call(
            f"{APIFY_API}/acts/{actor_path}/runs?{urlencode({'maxTotalChargeUsd': f'{max_charge_usd:.2f}', 'memory': 1024})}",
            method="POST",
            body=body,
        )
        run = (started or {}).get("data") or {}
        if not run.get("id"):
            return None, None
        deadline = self.settings.linkedin_wait_minutes * 60
        waited = 0
        while run.get("status") not in _FINISHED and waited < deadline:
            checked = await self._call(f"{APIFY_API}/actor-runs/{run['id']}?waitForFinish={WAIT_SECONDS}")
            if checked is None:
                await self.sleep(WAIT_SECONDS)
            run = (checked or {}).get("data") or run
            waited += WAIT_SECONDS
        if run.get("status") not in _FINISHED:
            await self._call(f"{APIFY_API}/actor-runs/{run['id']}/abort", method="POST")
            self.problem = "did not finish in time"
        cost = _usd(run.get("usageTotalUsd"))
        if run.get("status") not in _HAS_RESULTS:
            self.problem = self.problem or f"run {str(run.get('status')).lower()}"
            return None, cost
        dataset = run.get("defaultDatasetId")
        items = await self._call(f"{APIFY_API}/datasets/{dataset}/items?{urlencode({'clean': 'true', 'format': 'json'})}")
        if not isinstance(items, list):
            self.problem = self.problem or "results unreadable"
            return None, cost
        return [item for item in items if isinstance(item, dict)], cost

    async def _call(self, url: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        response = await self.gateway.fetch(
            FetchRequest(
                url=url,
                method=method,
                kind="api",
                headers=self.headers,
                timeout_s=WAIT_SECONDS + 5,
                body=json.dumps(body).encode("utf-8") if body is not None else None,
            )
        )
        if isinstance(response, FetchRefusal):
            self.problem = response.reason
            return None
        if response.status_code >= 400:
            self.problem = f"Apify answered {response.status_code}"
            return None
        try:
            return json.loads(response.body)
        except ValueError:
            self.problem = "Apify sent something that is not JSON"
            return None


def _usd(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None
