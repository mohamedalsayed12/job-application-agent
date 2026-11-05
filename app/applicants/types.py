"""Typed preference shapes. Salary lives only on ExpectedSalary / PreferenceInput."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ExpectedSalary:
    """Matching preference only (D3). Never passed to search or budget."""

    amount: Decimal
    currency: str
    period: str


@dataclass(frozen=True, slots=True)
class DiscoveryPreference:
    """Roles/locations/sector for search planning. Intentionally has no salary fields."""

    target_roles: tuple[str, ...]
    locations: tuple[str, ...]
    sector: str


@dataclass(frozen=True, slots=True)
class PreferenceInput:
    """Validated create/edit payload for applicant_preference."""

    target_roles: tuple[str, ...]
    locations: tuple[str, ...]
    sector: str
    expected_salary: ExpectedSalary | None
