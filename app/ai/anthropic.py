"""Anthropic Messages API through the fetch gateway.

Raw HTTP rather than the ``anthropic`` SDK: every outside call must go through
``FetchGateway`` (allowlist, SSRF guard, rate limits), which the SDK would bypass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
# Long structured answers (fit reports, tailored CVs) take well over the gateway's 10 s default.
AI_TIMEOUT_S = 150.0
# Interview labelling and pack synthesis; callers pass this explicitly.
INTERVIEW_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class AiCallResponse:
    payload: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass(frozen=True, slots=True)
class AiCallFailure:
    reason: str


def anthropic_api_key(settings: Settings) -> str | None:
    key = settings.anthropic_api_key.strip()
    if settings.ai_provider.strip().lower() != "anthropic" or not key:
        return None
    return key


def messages_body(
    *,
    system: str,
    user: str,
    schema: dict,
    model: str,
    max_tokens: int,
) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }


async def send_anthropic_request(
    gateway: FetchGateway,
    body: dict,
    *,
    api_key: str,
    fallback_model: str,
    timeout_s: float | None = None,
) -> AiCallResponse | AiCallFailure:
    request = FetchRequest(
        url=ANTHROPIC_MESSAGES_URL,
        method="POST",
        kind="api",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
        body=json.dumps(body).encode("utf-8"),
        timeout_s=AI_TIMEOUT_S if timeout_s is None else timeout_s,
    )
    outcome = await gateway.fetch(request)
    if isinstance(outcome, FetchRefusal):
        return AiCallFailure(reason=f"fetch:{outcome.reason}")
    return _parse_http(outcome, fallback_model=fallback_model)


def _parse_http(
    response: FetchResponse,
    *,
    fallback_model: str,
) -> AiCallResponse | AiCallFailure:
    if response.status_code == 429:
        return AiCallFailure(reason="anthropic_rate_limited")
    if response.status_code != 200:
        return AiCallFailure(reason=f"anthropic_http_{response.status_code}")
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return AiCallFailure(reason="anthropic_invalid_json")
    if not isinstance(data, dict):
        return AiCallFailure(reason="anthropic_invalid_json")

    # Refusal or truncation means the text may not match the schema.
    stop_reason = data.get("stop_reason")
    if stop_reason != "end_turn":
        return AiCallFailure(reason=f"anthropic_stop_{stop_reason}")

    content = data.get("content")
    if not isinstance(content, list):
        return AiCallFailure(reason="anthropic_bad_content")
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text.strip():
        return AiCallFailure(reason="anthropic_empty_content")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return AiCallFailure(reason="anthropic_content_not_json")
    if not isinstance(payload, dict):
        return AiCallFailure(reason="anthropic_content_not_object")

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    model = data.get("model")
    return AiCallResponse(
        payload=payload,
        prompt_tokens=_count(usage, "input_tokens"),
        completion_tokens=_count(usage, "output_tokens"),
        model=model if isinstance(model, str) and model else fallback_model,
    )


def _count(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) and value >= 0 else 0
