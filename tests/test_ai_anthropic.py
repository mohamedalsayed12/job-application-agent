"""D30: Anthropic provider for leftover extraction (mocked Messages API)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction import AiBudgetWait, AiTokenCapWait, extract_ai_leftovers
from app.extraction.ai_anthropic import ANTHROPIC_MESSAGES_URL, ANTHROPIC_VERSION
from app.extraction.ai_call import call_leftover_extraction
from app.extraction.ai_prompt import (
    EXTRACTION_JSON_SCHEMA,
    schema_without_string_limits,
)
from app.extraction.ai_types import AiCallFailure
from app.fetch.gateway import FetchGateway
from app.models import Candidate, SpendLedger
from tests.test_ai_extraction import (
    _add_results,
    _load,
    _policy,
    _public_resolve,
    _seed_run,
    _settings,
)

QUOTE = "Brightly Software is hiring backend engineers in Manchester"


def _anthropic_settings(**overrides: object):
    base: dict[str, object] = dict(
        ai_provider="anthropic",
        ai_extraction_model="",
        anthropic_api_key="test-anthropic-key",
    )
    base.update(overrides)
    return _settings(**base)


def _gateway(handler: object) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.anthropic.com"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def _message(employers: list[dict], *, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps({"employers": employers})}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 210, "output_tokens": 70},
    }


def test_default_provider_is_anthropic_haiku() -> None:
    cfg = _settings(ai_provider="anthropic", ai_extraction_model="")
    assert cfg.resolved_ai_extraction_model() == "claude-haiku-5-5"
    assert "api.anthropic.com" in cfg.api_allowlist_hosts()
    assert _settings(ai_provider="openai", ai_extraction_model="").resolved_ai_extraction_model() == "gpt-4o-mini"


def test_anthropic_schema_drops_string_limits_only() -> None:
    stripped = json.dumps(schema_without_string_limits(EXTRACTION_JSON_SCHEMA))
    assert "maxLength" in json.dumps(EXTRACTION_JSON_SCHEMA)
    assert "maxLength" not in stripped
    assert '"additionalProperties": false' in stripped


@pytest.mark.asyncio
async def test_missing_key_and_unknown_provider_fail_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    gateway = _gateway(handler)
    try:
        missing = await call_leftover_extraction(
            gateway, [], settings=_anthropic_settings(anthropic_api_key=""), max_output_tokens=500
        )
        unknown = await call_leftover_extraction(
            gateway, [], settings=_anthropic_settings(ai_provider="nope"), max_output_tokens=500
        )
    finally:
        await gateway.aclose()
    assert missing == AiCallFailure(reason="anthropic_missing_api_key")
    assert unknown == AiCallFailure(reason="unknown_ai_provider:nope")


@pytest.mark.asyncio
async def test_anthropic_request_shape_and_grounded_candidate(session: Session) -> None:
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("2.00"), now)
    run = _seed_run(session)
    rows = _add_results(session, run, _load("leftover_web_results.json")["results"])
    assert QUOTE in rows[0].snippet

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(ANTHROPIC_MESSAGES_URL)
        assert request.headers["x-api-key"] == "test-anthropic-key"
        assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "claude-haiku-5-5"
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert "maxLength" not in json.dumps(body["output_config"]["format"]["schema"])
        assert "DATA only" in body["messages"][0]["content"]
        assert "untrusted data" in body["system"]
        return httpx.Response(
            200,
            json=_message(
                [
                    {
                        "company_name": "Brightly Software",
                        "domain_candidates": ["brightlysoftware.example"],
                        "evidence": [{"result_id": str(rows[0].id), "quote": QUOTE}],
                    },
                    {
                        "company_name": "Invented Corp",
                        "domain_candidates": ["invented.example"],
                        "evidence": [{"result_id": str(rows[0].id), "quote": "not in the data"}],
                    },
                ]
            ),
        )

    gateway = _gateway(handler)
    try:
        outcome = await extract_ai_leftovers(
            session, gateway, rows, settings=_anthropic_settings(), related_run_id=run.id
        )
    finally:
        await gateway.aclose()

    assert not isinstance(outcome, (AiBudgetWait, AiTokenCapWait))
    assert [hit.company_name for hit in outcome.grounded] == ["Brightly Software"]
    assert outcome.grounded[0].domain_candidates == ("brightlysoftware.example",)
    assert outcome.model == "claude-haiku-5-5"
    assert outcome.prompt_tokens == 210 and outcome.completion_tokens == 70
    assert {item.reason for item in outcome.ungrounded} == {"quote_not_in_result"}

    candidate = session.scalars(select(Candidate)).one()
    assert candidate.domain_candidates == ["brightlysoftware.example"]
    ledger = session.scalars(select(SpendLedger)).one()
    assert ledger.provider == "anthropic"
    assert ledger.actual_cost == Decimal("0.0005")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, _message([], stop_reason="refusal"), "anthropic_stop_refusal"),
        (200, _message([], stop_reason="max_tokens"), "anthropic_stop_max_tokens"),
        (429, {"type": "error"}, "anthropic_rate_limited"),
        (401, {"type": "error"}, "anthropic_http_401"),
    ],
)
async def test_anthropic_failures_spend_nothing(
    session: Session, status: int, body: dict, reason: str
) -> None:
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    _policy(session, Decimal("2.00"), now)
    run = _seed_run(session)
    rows = _add_results(session, run, _load("leftover_web_results.json")["results"])

    gateway = _gateway(lambda request: httpx.Response(status, json=body))
    try:
        outcome = await extract_ai_leftovers(
            session, gateway, rows, settings=_anthropic_settings(), related_run_id=run.id
        )
    finally:
        await gateway.aclose()

    assert not isinstance(outcome, (AiBudgetWait, AiTokenCapWait))
    assert outcome.grounded == ()
    assert [item.reason for item in outcome.ungrounded] == [f"provider:{reason}"]
    assert session.scalars(select(Candidate)).all() == []
    assert all(
        row.actual_cost in (None, Decimal("0")) for row in session.scalars(select(SpendLedger))
    )
