"""Conservative unit prices until V3 / B4 land."""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.models import SpendKind

# Reed/Adzuna quotas and SerpAPI free-plan credits do not spend money_cap.
FREE_MONEY_PROVIDERS = frozenset({"reed", "adzuna", "serpapi"})


def estimate_cost(
    *,
    provider: str,
    kind: SpendKind,
    units: Decimal,
    settings: Settings,
) -> Decimal:
    if provider.lower() in FREE_MONEY_PROVIDERS and kind == SpendKind.search:
        return Decimal("0")
    if kind == SpendKind.search:
        unit_price = settings.search_request_unit_price
    elif kind == SpendKind.ai_extraction:
        unit_price = settings.ai_extraction_unit_price
    else:
        unit_price = settings.ai_summary_unit_price
    return (unit_price * units).quantize(Decimal("0.0001"))
