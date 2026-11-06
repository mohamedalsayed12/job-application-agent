"""OpenAI Chat Completions for leftover extraction via the fetch gateway (D30)."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.extraction.ai_prompt import EXTRACTION_JSON_SCHEMA, build_messages
from app.extraction.ai_types import LeftoverResult
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


@dataclass(frozen=True, slots=True)
class OpenAIExtractionResponse:
    payload: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass(frozen=True, slots=True)
class OpenAICallFailure:
    reason: str


async def call_leftover_extraction(
    gateway: FetchGateway,
    results: Sequence[LeftoverResult],
    *,
    settings: Settings | None = None,
    max_output_tokens: int | None = None,
) -> OpenAIExtractionResponse | OpenAICallFailure:
    cfg = settings or get_settings()
    api_key = cfg.openai_api_key.strip()
    if not api_key:
        return OpenAICallFailure(reason="openai_missing_api_key")
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
        return OpenAICallFailure(reason=f"fetch:{outcome.reason}")
    return _parse_http(outcome, fallback_model=model)


def _parse_http(
    response: FetchResponse,
    *,
    fallback_model: str,
) -> OpenAIExtractionResponse | OpenAICallFailure:
    if response.status_code == 429:
        return OpenAICallFailure(reason="openai_rate_limited")
    if response.status_code in {401, 403}:
        return OpenAICallFailure(reason=f"openai_http_{response.status_code}")
    if response.status_code >= 500:
        return OpenAICallFailure(reason=f"openai_http_{response.status_code}")
    if response.status_code != 200:
        return OpenAICallFailure(reason=f"openai_http_{response.status_code}")
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return OpenAICallFailure(reason="openai_invalid_json")
    if not isinstance(data, dict):
        return OpenAICallFailure(reason="openai_invalid_json")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return OpenAICallFailure(reason="openai_empty_choices")
    first = choices[0]
    if not isinstance(first, dict):
        return OpenAICallFailure(reason="openai_bad_choice")
    message = first.get("message")
    if not isinstance(message, dict):
        return OpenAICallFailure(reason="openai_bad_message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return OpenAICallFailure(reason="openai_empty_content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return OpenAICallFailure(reason="openai_content_not_json")
    if not isinstance(payload, dict):
        return OpenAICallFailure(reason="openai_content_not_object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_tokens = _int_field(usage, "prompt_tokens")
    completion_tokens = _int_field(usage, "completion_tokens")
    model = data.get("model")
    return OpenAIExtractionResponse(
        payload=payload,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model if isinstance(model, str) and model else fallback_model,
    )


def estimate_input_tokens(results: Sequence[LeftoverResult]) -> int:
    """Rough token estimate for the budget gate before the paid call."""
    from app.extraction.ai_prompt import SYSTEM_INSTRUCTIONS, wrap_results_as_data

    text = SYSTEM_INSTRUCTIONS + "\n" + wrap_results_as_data(results)
    # ~4 chars/token is a coarse upper bound for English JSON wrappers.
    return max(1, (len(text) + 3) // 4)


def _int_field(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return 0
