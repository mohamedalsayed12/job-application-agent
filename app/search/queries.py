"""Broad cold-start queries from DiscoveryPreference only (D3 / T1)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from app.applicants.types import DiscoveryPreference
from app.search.templates import TemplateSpec, get_template_spec


@dataclass(frozen=True, slots=True)
class BuiltQuery:
    text: str
    location: str | None = None
    site: str | None = None


def build_query(
    template_id: str,
    pref: DiscoveryPreference,
    *,
    employer_name: str | None = None,
) -> BuiltQuery:
    return build_queries(template_id, pref, employer_name=employer_name, limit=1)[0]


def build_queries(
    template_id: str,
    pref: DiscoveryPreference,
    *,
    employer_name: str | None = None,
    limit: int,
) -> tuple[BuiltQuery, ...]:
    """Up to ``limit`` different queries, one for each role and location pair."""
    spec = get_template_spec(template_id)
    built = (
        _build_for_spec(spec, role=role.strip(), location=location.strip(), employer_name=employer_name)
        for role, location in product(pref.target_roles, pref.locations)
    )
    return tuple(dict.fromkeys(built))[:limit]


def _build_for_spec(
    spec: TemplateSpec,
    *,
    role: str,
    location: str,
    employer_name: str | None,
) -> BuiltQuery:
    if spec.id == "CF1":
        return BuiltQuery(text=role, location=location)
    if spec.id == "CF1b":
        return BuiltQuery(text=role, location=location)
    if spec.id == "CF2":
        return BuiltQuery(text=f"{role} {location} hiring OR careers OR jobs")
    if spec.id == "CF3":
        if not employer_name or not employer_name.strip():
            raise ValueError("CF3 requires employer_name")
        # Step 10 audit C3: location and OR pulled in city pages and job boards.
        return BuiltQuery(text=f'"{employer_name.strip()}" official website')
    if spec.id in {"BD1", "BD2"}:
        return BuiltQuery(text=f"{role} {location}", site=spec.site)
    raise ValueError(f"unknown template_id: {spec.id}")
