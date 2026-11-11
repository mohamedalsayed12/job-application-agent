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
    elif kind == SpendKind.ai_cv_read:
        unit_price = settings.ai_cv_unit_price
    elif kind == SpendKind.ai_job_judge:
        unit_price = settings.ai_judge_unit_price
    elif kind == SpendKind.ai_fit_report:
        unit_price = settings.ai_report_unit_price
    else:
        unit_price = settings.ai_summary_unit_price
    return (unit_price * units).quantize(Decimal("0.0001"))
