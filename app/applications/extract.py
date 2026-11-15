"""Pull application form schemas: Greenhouse public API first, Apify for the rest."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.applications.schema import (
    FormSchema,
    classify_ats,
    fields_from_apify_items,
    greenhouse_questions_to_schema,
    parse_greenhouse_board,
)
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest
from app.models import AtsKind, SpendKind

logger = logging.getLogger(__name__)
APIFY_API = "https://api.apify.com/v2"


@dataclass(frozen=True, slots=True)
class ExtractResult:
    schema: FormSchema | None
    error: str | None = None
    budget_blocked: bool = False


async def extract_form_schema(
    session: Session,
    *,
    apply_url: str,
    gateway: FetchGateway,
    settings: Settings,
    persist,
    now=lambda: datetime.now(UTC),
) -> ExtractResult:
    ats = classify_ats(apply_url)
    if ats is AtsKind.greenhouse:
        parsed = parse_greenhouse_board(apply_url)
        if parsed:
            board, job_id = parsed
            result = await _greenhouse_api(gateway, board, job_id)
            if result.schema is not None:
                return result
            logger.info("Greenhouse API missed %s/%s: %s", board, job_id, result.error)

    token = settings.apify_api_token.strip()
    if not token:
        return ExtractResult(None, error="Apify is not configured for form extraction.")

    gate = BudgetGate(session, clock=now)
    estimate = settings.apply_form_unit_price + settings.apply_form_run_price
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider="apify",
                kind=SpendKind.apply_form,
                units=Decimal(1),
                unit_label="form",
                estimated_cost=estimate,
            )
        )
    except BudgetWait:
        return ExtractResult(None, error="Monthly budget reached.", budget_blocked=True)
    persist()

    run = _ApifyFormRun(gateway, token, settings)
    items, cost_usd, problem = await run.extract(apply_url, ats.value)
    actual = estimate if cost_usd is None else (cost_usd * settings.usd_to_gbp).quantize(Decimal("0.0001"))
    gate.settle(reservation, actual_cost=actual, units=Decimal(1))
    persist()
    if items is None:
        return ExtractResult(None, error=problem or "Form extraction failed.")
    return ExtractResult(fields_from_apify_items(items, ats=ats))


async def _greenhouse_api(gateway: FetchGateway, board: str, job_id: str) -> ExtractResult:
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote(board)}/jobs/{quote(job_id)}?questions=true"
    response = await gateway.fetch(FetchRequest(url=url, kind="api", timeout_s=15.0))
    if isinstance(response, FetchRefusal):
        return ExtractResult(None, error=f"Greenhouse API refused: {response.reason}")
    if response.status_code >= 400:
        return ExtractResult(None, error=f"Greenhouse API returned {response.status_code}.")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ExtractResult(None, error=f"Greenhouse API body was not JSON: {exc}")
    if not isinstance(payload, dict):
        return ExtractResult(None, error="Greenhouse API returned an unexpected shape.")
    return ExtractResult(greenhouse_questions_to_schema(payload))


class _ApifyFormRun:
    def __init__(self, gateway: FetchGateway, token: str, settings: Settings) -> None:
        self.gateway = gateway
        self.token = token
        self.settings = settings
        self.problem: str | None = None

    async def extract(self, apply_url: str, ats: str) -> tuple[list[dict[str, Any]] | None, Decimal | None, str | None]:
        actor = self.settings.apply_form_actor.strip()
        if not actor:
            self.problem = "APPLY_FORM_ACTOR is not set."
            return None, None, self.problem
        start = await self.gateway.fetch(
            FetchRequest(
                url=f"{APIFY_API}/acts/{quote(actor, safe='')}/runs?token={self.token}",
                method="POST",
                kind="api",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"applyUrl": apply_url, "ats": ats}).encode(),
                timeout_s=20.0,
            )
        )
        if isinstance(start, FetchRefusal) or start.status_code >= 300:
            self.problem = "Could not start the Apify form actor."
            return None, None, self.problem
        data = json.loads(start.body.decode()).get("data") or {}
        run_id = data.get("id")
        dataset = data.get("defaultDatasetId")
        if not run_id or not dataset:
            self.problem = "Apify did not return a run id."
            return None, None, self.problem

        cost_usd: Decimal | None = None
        for _ in range(max(1, self.settings.apply_form_wait_minutes * 4)):
            check = await self.gateway.fetch(
                FetchRequest(
                    url=f"{APIFY_API}/actor-runs/{run_id}?token={self.token}",
                    kind="api",
                    timeout_s=25.0,
                )
            )
            if isinstance(check, FetchRefusal):
                self.problem = "Apify run check refused."
                return None, None, self.problem
            payload = json.loads(check.body.decode()).get("data") or {}
            status = payload.get("status")
            if payload.get("usageTotalUsd") is not None:
                cost_usd = Decimal(str(payload["usageTotalUsd"]))
            if status == "SUCCEEDED":
                items_resp = await self.gateway.fetch(
                    FetchRequest(
                        url=f"{APIFY_API}/datasets/{dataset}/items?token={self.token}",
                        kind="api",
                        timeout_s=20.0,
                    )
                )
                if isinstance(items_resp, FetchRefusal):
                    self.problem = "Could not read Apify dataset."
                    return None, cost_usd, self.problem
                items = json.loads(items_resp.body.decode())
                if not isinstance(items, list):
                    self.problem = "Apify dataset was not a list."
                    return None, cost_usd, self.problem
                return items, cost_usd, None
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                self.problem = f"Apify run {status}."
                return None, cost_usd, self.problem
            await asyncio.sleep(15)
        await self.gateway.fetch(
            FetchRequest(
                url=f"{APIFY_API}/actor-runs/{run_id}/abort?token={self.token}",
                method="POST",
                kind="api",
                timeout_s=15.0,
            )
        )
        self.problem = "Apify form run timed out."
        return None, cost_usd, self.problem
