"""Send one leftover-extraction call to the configured AI_PROVIDER (D30). Only Anthropic is built."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.config import Settings
from app.extraction.ai_anthropic import call_anthropic_extraction
from app.extraction.ai_types import AiCallFailure, AiCallResponse, LeftoverResult
from app.fetch.gateway import FetchGateway

_PROVIDERS = {
    "anthropic": call_anthropic_extraction,
}


async def call_leftover_extraction(
    gateway: FetchGateway,
    results: Sequence[LeftoverResult],
    *,
    settings: Settings,
    max_output_tokens: int,
) -> AiCallResponse | AiCallFailure:
    provider = settings.ai_provider.strip().lower()
    call = _PROVIDERS.get(provider)
    if call is None:
        return AiCallFailure(reason=f"unknown_ai_provider:{provider}")
    return await call(
        gateway,
        results,
        settings=settings,
        max_output_tokens=max_output_tokens,
    )
