"""Pull application form schemas: Greenhouse public API first, Apify for the rest."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

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
WAIT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class ExtractResult:
    schema: FormSchema | None
    error: str | None = None
    budget_blocked: bool = False


def _usd(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None


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

    cost_usd: Decimal | None = None
    items: list[dict[str, Any]] | None = None
    problem: str | None = None
    try:
        run = _ApifyFormRun(gateway, token, settings)
        items, cost_usd, problem = await run.extract(apply_url, ats.value)
    except Exception:
        logger.exception("Apify form extraction crashed for %s", apply_url)
        problem = "Form extraction crashed."
        items = None
    finally:
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
        self.settings = settings
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.problem: str | None = None

    async def extract(self, apply_url: str, ats: str) -> tuple[list[dict[str, Any]] | None, Decimal | None, str | None]:
        actor = self.settings.apply_form_actor.strip()
        if not actor:
            self.problem = "APPLY_FORM_ACTOR is not set."
            return None, None, self.problem
        actor_path = quote(actor.replace("/", "~"), safe="~")
        data = await self._call(f"{APIFY_API}/acts/{actor_path}/runs", method="POST", body={"applyUrl": apply_url, "ats": ats})
        run = (data or {}).get("data") or {}
        run_id = run.get("id")
        dataset = run.get("defaultDatasetId")
        if not run_id or not dataset:
            self.problem = self.problem or "Apify did not return a run id."
            return None, None, self.problem

        cost_usd: Decimal | None = None
        deadline = max(1, self.settings.apply_form_wait_minutes) * 60
        waited = 0
        while waited < deadline:
            checked = await self._call(f"{APIFY_API}/actor-runs/{run_id}")
            payload = (checked or {}).get("data") or {}
            status = payload.get("status")
            cost_usd = _usd(payload.get("usageTotalUsd")) or cost_usd
            if status == "SUCCEEDED":
                items = await self._call(f"{APIFY_API}/datasets/{dataset}/items?{urlencode({'clean': 'true', 'format': 'json'})}")
                if not isinstance(items, list):
                    self.problem = "Apify dataset was not a list."
                    return None, cost_usd, self.problem
                return items, cost_usd, None
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                self.problem = f"Apify run {status}."
                return None, cost_usd, self.problem
            await asyncio.sleep(WAIT_SECONDS)
            waited += WAIT_SECONDS
        await self._call(f"{APIFY_API}/actor-runs/{run_id}/abort", method="POST")
        self.problem = "Apify form run timed out."
        return None, cost_usd, self.problem

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
            self.problem = f"Apify refused: {response.reason}"
            return None
        if response.status_code >= 400:
            self.problem = f"Apify answered {response.status_code}"
            return None
        try:
            return json.loads(response.body)
        except ValueError:
            self.problem = "Apify sent something that is not JSON"
            return None
