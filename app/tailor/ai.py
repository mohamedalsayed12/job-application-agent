"""The five Claude calls of the tailoring pipeline.

Each mirrors one Resume-Matcher LLM call (srbhr/Resume-Matcher@959e458): parse_resume_to_json in
services/parser.py, extract_job_keywords and generate_resume_diffs in services/improver.py,
inject_keywords in services/refiner.py, and generate_cover_letter in services/cover_letter.py. Structured outputs fix each reply's shape, so the code
below trusts the types and checks only meaning.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai.anthropic import AiCallFailure, AiCallResponse, send_anthropic_request
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.tailor import prompts
from app.tailor.improver import CHANGE_SCHEMA, Change, changes_from
from app.tailor.resume import RESUME_SCHEMA, SECTIONS, ground_parsed
from app.tailor.voice import MAX_REWRITES

MAX_INJECT_POSTING_CHARS = 2000
_LISTS = (
    "required_skills",
    "preferred_skills",
    "experience_requirements",
    "education_requirements",
    "key_responsibilities",
    "keywords",
)
KEYWORDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "company": {"type": "string"},
        "role": {"type": "string"},
        **{field: {"type": "array", "items": {"type": "string"}} for field in _LISTS},
        "experience_years": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "seniority_level": {"type": "string"},
    },
    "required": ["company", "role", *_LISTS, "experience_years", "seniority_level"],
}
COVER_LETTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"letter": {"type": "string"}},
    "required": ["letter"],
}


async def parse_master(
    gateway: FetchGateway, cv: str, *, settings: Settings, api_key: str
) -> tuple[dict[str, Any] | None, AiCallResponse] | AiCallFailure:
    """The grounded master CV, or None when nothing the parse returned is in the CV text."""
    prompt = prompts.PARSE_RESUME_PROMPT.format(schema=prompts.RESUME_SCHEMA_EXAMPLE, resume_text=cv)
    call = await _ask(gateway, prompts.PARSE_SYSTEM, prompt, RESUME_SCHEMA, settings=settings, api_key=api_key)
    if isinstance(call, AiCallFailure):
        return call
    return ground_parsed(call.payload, cv), call


async def extract_job_keywords(
    gateway: FetchGateway, posting: str, *, settings: Settings, api_key: str
) -> tuple[dict[str, Any], AiCallResponse] | AiCallFailure:
    prompt = prompts.EXTRACT_KEYWORDS_PROMPT.format(job_description=posting)
    call = await _ask(gateway, prompts.KEYWORDS_SYSTEM, prompt, KEYWORDS_SCHEMA, settings=settings, api_key=api_key)
    if isinstance(call, AiCallFailure):
        return call
    keywords = {
        **call.payload,
        **{field: [item.strip() for item in call.payload[field] if item.strip()] for field in _LISTS},
    }
    return keywords, call


async def generate_resume_diffs(
    gateway: FetchGateway,
    *,
    master: dict[str, Any],
    posting: str,
    keywords: dict[str, Any],
    settings: Settings,
    api_key: str,
) -> tuple[list[Change], AiCallResponse] | AiCallFailure:
    prompt = prompts.DIFF_IMPROVE_PROMPT.format(
        strategy_instruction=prompts.KEYWORDS_STRATEGY,
        max_changes=MAX_REWRITES,
        output_language="British English",
        job_keywords=keywords_for_prompt(keywords),
        job_description=posting,
        original_resume=json.dumps(master, ensure_ascii=False),
    )
    call = await _ask(gateway, prompts.DIFF_SYSTEM, prompt, CHANGE_SCHEMA, settings=settings, api_key=api_key)
    if isinstance(call, AiCallFailure):
        return call
    return changes_from(call.payload), call


async def inject_keywords(
    gateway: FetchGateway,
    *,
    tailored: dict[str, Any],
    keywords: list[str],
    master: dict[str, Any],
    posting: str,
    settings: Settings,
    api_key: str,
) -> tuple[dict[str, Any] | None, AiCallResponse] | AiCallFailure:
    """The rewritten CV, or None when the rewrite emptied a section the CV had."""
    prompt = prompts.KEYWORD_INJECTION_PROMPT.format(
        keywords_to_inject=json.dumps(keywords),
        current_resume=json.dumps(tailored),
        master_resume=json.dumps(master),
        job_description=posting[:MAX_INJECT_POSTING_CHARS],
    )
    call = await _ask(gateway, prompts.INJECT_SYSTEM, prompt, RESUME_SCHEMA, settings=settings, api_key=api_key)
    if isinstance(call, AiCallFailure):
        return call
    if any(tailored.get(section) and not call.payload.get(section) for section in SECTIONS):
        return None, call
    return call.payload, call


async def generate_cover_letter(
    gateway: FetchGateway,
    *,
    resume: dict[str, Any],
    posting: str,
    settings: Settings,
    api_key: str,
    voice: str = "",
) -> tuple[str | None, AiCallResponse] | AiCallFailure:
    """A letter written from the tailored CV in the voice of `voice`, the person's own sentences; None when blank."""
    prompt = prompts.COVER_LETTER_PROMPT.format(
        output_language="British English",
        job_description=posting,
        resume_data=json.dumps(resume, ensure_ascii=False),
        voice_sample=voice or "(none supplied)",
    )
    call = await _ask(
        gateway, prompts.COVER_LETTER_SYSTEM, prompt, COVER_LETTER_SCHEMA, settings=settings, api_key=api_key
    )
    if isinstance(call, AiCallFailure):
        return call
    return call.payload["letter"].strip() or None, call


def keywords_for_prompt(keywords: dict[str, Any]) -> str:
    sections = []
    if keywords.get("required_skills"):
        sections.append("Required skills to emphasize:\n- " + "\n- ".join(keywords["required_skills"]))
    if keywords.get("preferred_skills"):
        sections.append(
            "Preferred skills (include only if resume supports them):\n- " + "\n- ".join(keywords["preferred_skills"])
        )
    if keywords.get("keywords"):
        sections.append("Additional keywords to weave in naturally:\n- " + "\n- ".join(keywords["keywords"]))
    return "\n\n".join(sections) or "No specific keywords extracted."


async def _ask(
    gateway: FetchGateway, system: str, prompt: str, schema: dict, *, settings: Settings, api_key: str
) -> AiCallResponse | AiCallFailure:
    model = settings.resolved_ai_tailor_model()
    body = {
        "model": model,
        "max_tokens": settings.ai_tailor_max_output_tokens,
        "thinking": {"type": "disabled"},
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    return await send_anthropic_request(gateway, body, api_key=api_key, fallback_model=model)
