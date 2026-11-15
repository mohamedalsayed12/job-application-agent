from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.models import SpendKind

_PRICE = {
    SpendKind.linkedin_jobs: "linkedin_unit_price",
    SpendKind.ai_cv_read: "ai_cv_unit_price",
    SpendKind.ai_job_judge: "ai_judge_unit_price",
    SpendKind.ai_fit_report: "ai_report_unit_price",
    SpendKind.ai_summary: "ai_summary_unit_price",
}


def estimate_cost(*, kind: SpendKind, units: Decimal, settings: Settings) -> Decimal:
    attr = _PRICE.get(kind)
    if attr is None:
        raise ValueError(f"no unit price for spend kind {kind}")
    return (getattr(settings, attr) * units).quantize(Decimal("0.0001"))
