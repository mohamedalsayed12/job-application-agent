"""OpenAI Chat Completions for leftover extraction via the fetch gateway (D30)."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from app.core.config import Settings, get_settings
from app.extraction.ai_prompt import EXTRACTION_JSON_SCHEMA, build_messages
from app.extraction.ai_types import AiCallFailure, AiCallResponse, LeftoverResult
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


async def call_openai_extraction(
    gateway: FetchGateway,
    results: Sequence[LeftoverResult],
    *,
    settings: Settings | None = None,
    max_output_tokens: int | None = None,
) -> AiCallResponse | AiCallFailure:
    cfg = settings or get_settings()
    api_key = cfg.openai_api_key.strip()
    if not api_key:
        return AiCallFailure(reason="openai_missing_api_key")
    model = cfg.ai_extraction_model.strip() or "gpt-4o-mini"
    body = {
        "model": model,
        "messages": build_messages(results),
        "temperature": 0,
        "max_tokens": max_output_tokens
        if max_output_tokens is not None
        else cfg.ai_extraction_max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "leftover_extraction",
                "strict": True,
                "schema": EXTRACTION_JSON_SCHEMA,
            },
        },
    }
    outcome = await gateway.fetch(
        FetchRequest(
            url=OPENAI_CHAT_URL,
            method="POST",
            kind="api",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(body).encode("utf-8"),
        )
    )
    if isinstance(outcome, FetchRefusal):
        return AiCallFailure(reason=f"fetch:{outcome.reason}")
    return _parse_http(outcome, fallback_model=model)


def _parse_http(
    response: FetchResponse,
    *,
    fallback_model: str,
) -> AiCallResponse | AiCallFailure:
    if response.status_code == 429:
        return AiCallFailure(reason="openai_rate_limited")
    if response.status_code in {401, 403}:
        return AiCallFailure(reason=f"openai_http_{response.status_code}")
    if response.status_code >= 500:
        return AiCallFailure(reason=f"openai_http_{response.status_code}")
    if response.status_code != 200:
        return AiCallFailure(reason=f"openai_http_{response.status_code}")
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return AiCallFailure(reason="openai_invalid_json")
    if not isinstance(data, dict):
        return AiCallFailure(reason="openai_invalid_json")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return AiCallFailure(reason="openai_empty_choices")
    first = choices[0]
    if not isinstance(first, dict):
        return AiCallFailure(reason="openai_bad_choice")
    message = first.get("message")
    if not isinstance(message, dict):
        return AiCallFailure(reason="openai_bad_message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return AiCallFailure(reason="openai_empty_content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return AiCallFailure(reason="openai_content_not_json")
    if not isinstance(payload, dict):
        return AiCallFailure(reason="openai_content_not_object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_tokens = _int_field(usage, "prompt_tokens")
    completion_tokens = _int_field(usage, "completion_tokens")
    model = data.get("model")
    return AiCallResponse(
        payload=payload,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model if isinstance(model, str) and model else fallback_model,
    )


def _int_field(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return 0
