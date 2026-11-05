"""Conservative unit prices until V3 / B4 land."""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.models import SpendKind

# Providers whose search calls are free of the monthly money cap (Reed / Adzuna).
FREE_MONEY_PROVIDERS = frozenset({"reed", "adzuna"})


def estimate_cost(
    *,
    provider: str,
    kind: SpendKind | str,
    units: Decimal,
    settings: Settings,
) -> Decimal:
    kind_value = kind.value if isinstance(kind, SpendKind) else kind
    if provider.lower() in FREE_MONEY_PROVIDERS and kind_value == SpendKind.search:
        return Decimal("0")
    if kind_value == SpendKind.search:
        unit_price = settings.search_request_unit_price
    elif kind_value == SpendKind.ai_extraction:
        unit_price = settings.ai_extraction_unit_price
    elif kind_value == SpendKind.ai_summary:
        unit_price = settings.ai_summary_unit_price
    else:
        unit_price = settings.search_request_unit_price
    return (unit_price * units).quantize(Decimal("0.0001"))


def needs_money_cap(provider: str, kind: SpendKind | str, estimated_cost: Decimal) -> bool:
    kind_value = kind.value if isinstance(kind, SpendKind) else kind
    if provider.lower() in FREE_MONEY_PROVIDERS and kind_value == SpendKind.search:
        return False
    return estimated_cost > 0
