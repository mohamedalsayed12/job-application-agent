"""Project preferences for search/budget callers — salary columns stay off this type (D3)."""

from __future__ import annotations

from app.applicants.types import DiscoveryPreference
from app.models import ApplicantPreference


def to_discovery_preference(pref: ApplicantPreference) -> DiscoveryPreference:
    """Build a discovery view. Callers that only hold this type cannot read salary."""
    return DiscoveryPreference(
        target_roles=tuple(pref.target_roles),
        locations=tuple(pref.locations),
        sector=pref.sector,
    )
