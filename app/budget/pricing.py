from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.models import SpendKind

_PRICE = {
    SpendKind.linkedin_jobs: "linkedin_unit_price",
    SpendKind.indeed_jobs: "indeed_unit_price",
    SpendKind.linkedin_people: "outreach_people_unit_price",
    SpendKind.linkedin_profile: "outreach_profile_unit_price",
    SpendKind.ai_outreach: "outreach_ai_unit_price",
    SpendKind.interview_collect: "interview_collect_unit_price",
    SpendKind.ai_interview_analyse: "interview_analyse_unit_price",
    SpendKind.ai_interview_plan: "interview_plan_unit_price",
    SpendKind.ai_interview_tailor: "interview_tailor_unit_price",
    SpendKind.ai_cv_read: "ai_cv_unit_price",
    SpendKind.ai_job_judge: "ai_judge_unit_price",
    SpendKind.ai_fit_report: "ai_report_unit_price",
    SpendKind.ai_cv_tailor: "ai_tailor_unit_price",
    SpendKind.ai_summary: "ai_summary_unit_price",
}


def estimate_cost(*, kind: SpendKind, units: Decimal, settings: Settings, provider: str = "") -> Decimal:
    del provider  # Call sites still pass it; price is keyed only by kind.
    attr = _PRICE.get(kind)
    if attr is None:
        raise ValueError(f"no unit price for spend kind {kind}")
    return (getattr(settings, attr) * units).quantize(Decimal("0.0001"))
