"""Search-facing helpers. Only DiscoveryPreference — never ExpectedSalary (D3, T1 seed)."""

from __future__ import annotations

from app.applicants.types import DiscoveryPreference


def build_role_location_query(pref: DiscoveryPreference) -> str:
    """Compose a discovery query from roles and locations only."""
    roles = " OR ".join(pref.target_roles)
    locations = " OR ".join(pref.locations)
    return f"({roles}) ({locations}) {pref.sector}"
