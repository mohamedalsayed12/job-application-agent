"""Map the fields on the open employer page to a fill plan.

Saved answers and CV quotes first (D66/D70). Then the question bank and checked AI drafts (D75).
The model may point at a fact the person gave, or quote their CV. It may not invent text for the map
step. Drafting is a separate, reviewed path that never covers health, identity, pay or declarations.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallFailure, send_anthropic_request
from app.applicants.apply_answers import answers_as_map, load_answers, saved_pairs
from app.applicants.cv_read import CvUnreadable, cv_text
from app.applications.cv_choice import choose_cv
from app.applications.drafts import draft_with_ai, fill_from_bank, remember_unanswered
from app.applications.mapper import (
    Fact,
    FillPlan,
    FormField,
    Match,
    build_fill_plan,
    facts_from_profile,
    matches_from_payload,
    unmatched,
)
from app.applications.prepare import store_fill_outcome
from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.core.config import get_settings
from app.fetch.gateway import FetchGateway
from app.models import Applicant, Application, CvFileType, Job, SpendKind

logger = logging.getLogger(__name__)

_CV_CHARS = 12_000
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matches"],
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field_id", "fact_id", "cv_quote"],
                "properties": {
                    "field_id": {"type": "string"},
                    "fact_id": {"type": "string"},
                    "cv_quote": {"type": "string"},
                },
            },
        }
    },
}
_SYSTEM = (
    "Match each form question to one fact id, or copy a short span from the CV into cv_quote. "
    "Leave both empty when nothing the person gave answers the question. Never write new words."
)


def notice_for(plan: FillPlan) -> str:
    if plan.missing:
        names = "; ".join(question.label for question in plan.missing)
        return f"These questions need an answer before filling: {names}"
    notice = "Everything is filled in. Review the form and click Submit yourself."
    if plan.attach:
        notice += " Attach these yourself: " + "; ".join(plan.attach) + "."
    return notice


def _plain(content: bytes | None, file_type: str | None) -> str:
    if not content or not file_type:
        return ""
    try:
        return cv_text(content, CvFileType(file_type), max_chars=_CV_CHARS)
    except (CvUnreadable, ValueError):
        return ""


def _remember(application: Application, plan: FillPlan, drafts: dict[str, Any] | None = None) -> FillPlan:
    """Store the plan. Steps stay even when some questions are open so the extension can fill what it can (D75)."""
    store_fill_outcome(application, plan, drafts=drafts)
    return plan


async def model_matches(
    session: Session,
    applicant_id: uuid.UUID,
    application_id: uuid.UUID,
    fields: list[FormField],
    facts: list[Fact],
    cv_text_value: str,
) -> dict[str, Match]:
    """Ask the model which fact or CV span answers each leftover question. Empty when it cannot run."""
    settings = get_settings()
    if not fields or not settings.anthropic_api_key.strip():
        return {}
    model = settings.resolved_ai_apply_map_model()
    if not model:
        return {}
    gate = BudgetGate(session)
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider=settings.ai_provider,
                kind=SpendKind.ai_apply_map,
                units=Decimal(1),
                unit_label="apply_map",
                estimated_cost=settings.ai_apply_map_unit_price,
                related_item_id=application_id,
                applicant_id=applicant_id,
            )
        )
    except BudgetWait:
        logger.warning("apply map skipped for %s: budget", application_id)
        return {}
    prompt = {
        "questions": [{"id": field.id, "question": field.question, "options": field.options} for field in fields],
        "facts": [{"id": fact.id, "question": fact.question} for fact in facts],
        "cv_text": cv_text_value[:_CV_CHARS],
    }
    call = await send_anthropic_request(
        FetchGateway(),
        {
            "model": model,
            "max_tokens": settings.ai_apply_map_max_output_tokens,
            "thinking": {"type": "disabled"},
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": json.dumps(prompt)}],
            "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
        },
        api_key=settings.anthropic_api_key,
        fallback_model=model,
    )
    if isinstance(call, AiCallFailure):
        gate.settle(reservation, consumed=call.billed)
        logger.warning("apply map failed for %s: %s", application_id, call.reason)
        return {}
    gate.settle(reservation, actual_cost=settings.ai_apply_map_unit_price, units=Decimal(1))
    return matches_from_payload(call.payload)


async def build_application_plan(
    session: Session,
    applicant: Applicant,
    application: Application,
    fields: list[FormField],
    *,
    gateway: FetchGateway | None = None,
) -> FillPlan:
    """Saved answers and CV map first, then the question bank, then checked AI drafts (D75)."""
    settings = get_settings()
    chosen = choose_cv(session, applicant.id, application)
    text = _plain(chosen.content if chosen else None, chosen.file_type if chosen else None)
    row = load_answers(session, applicant.id)
    answers = answers_as_map(row, email=applicant.email)
    facts = facts_from_profile(answers, saved_pairs(row))
    left = unmatched(fields, facts)
    matches = await model_matches(session, applicant.id, application.id, left, facts, text)
    plan = build_fill_plan(fields, facts, cv_text=text, matches=matches)
    plan = fill_from_bank(session, applicant.id, fields, plan)
    previous = (application.fill_plan_json or {}).get("drafts") if application.fill_plan_json else None
    drafts: dict[str, Any] = dict(previous or {})
    if gateway is not None:
        plan, drafts = await draft_with_ai(
            session,
            applicant,
            application,
            session.get(Job, application.job_id),
            fields,
            plan,
            answers,
            gateway=gateway,
            settings=settings,
            persist=session.flush,
            previous=previous if isinstance(previous, dict) else None,
        )
    remember_unanswered(session, applicant.id, fields, plan)
    shown = _remember(application, plan, drafts=drafts)
    session.flush()
    return shown
