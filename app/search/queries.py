"""Broad cold-start queries from DiscoveryPreference only (D3 / T1)."""

from __future__ import annotations

from dataclasses import dataclass

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
    spec = get_template_spec(template_id)
    role = pref.target_roles[0].strip()
    location = pref.locations[0].strip()
    return _build_for_spec(spec, role=role, location=location, employer_name=employer_name)


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
        name = employer_name.strip()
        return BuiltQuery(text=f'"{name}" careers OR "official site" {location}')
    if spec.id in {"BD1", "BD2"}:
        return BuiltQuery(text=f"{role} {location}", site=spec.site)
    raise ValueError(f"unknown template_id: {spec.id}")
