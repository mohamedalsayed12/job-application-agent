"""Conservative unit prices until V3 / B4 land."""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.models import SpendKind


def estimate_cost(
    *,
    provider: str,
    kind: SpendKind,
    units: Decimal,
    settings: Settings,
) -> Decimal:
    if kind == SpendKind.linkedin_jobs:
        unit_price = settings.linkedin_unit_price
    elif kind == SpendKind.indeed_jobs:
        unit_price = settings.indeed_unit_price
    elif kind == SpendKind.careers_search:
        unit_price = settings.careers_search_price
    elif kind == SpendKind.linkedin_people:
        unit_price = settings.outreach_people_unit_price
    elif kind == SpendKind.linkedin_profile:
        unit_price = settings.outreach_profile_unit_price
    elif kind == SpendKind.ai_outreach:
        unit_price = settings.outreach_ai_unit_price
    elif kind == SpendKind.interview_collect:
        unit_price = settings.interview_collect_unit_price
    elif kind == SpendKind.ai_interview_analyse:
        unit_price = settings.interview_analyse_unit_price
    elif kind == SpendKind.ai_interview_plan:
        unit_price = settings.interview_plan_unit_price
    elif kind == SpendKind.ai_interview_tailor:
        unit_price = settings.interview_tailor_unit_price
    elif kind == SpendKind.ai_cv_read:
        unit_price = settings.ai_cv_unit_price
    elif kind == SpendKind.ai_job_judge:
        unit_price = settings.ai_judge_unit_price
    elif kind == SpendKind.ai_fit_report:
        unit_price = settings.ai_report_unit_price
    elif kind == SpendKind.ai_cv_tailor:
        unit_price = settings.ai_tailor_unit_price
    elif kind == SpendKind.ai_search_ideas:
        unit_price = settings.ai_ideas_unit_price
    else:
        unit_price = settings.ai_summary_unit_price
    return (unit_price * units).quantize(Decimal("0.0001"))
