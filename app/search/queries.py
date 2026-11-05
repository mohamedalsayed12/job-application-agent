"""Broad cold-start queries from DiscoveryPreference only (D3 / T1)."""

from __future__ import annotations

from dataclasses import dataclass

from app.applicants.types import DiscoveryPreference

GREENHOUSE_SITE = "boards.greenhouse.io"
LEVER_SITES = ("jobs.lever.co", "jobs.eu.lever.co")


@dataclass(frozen=True, slots=True)
class BuiltQuery:
    text: str
    site: str | None = None


def _role(pref: DiscoveryPreference) -> str:
    return pref.target_roles[0].strip()


def _location(pref: DiscoveryPreference) -> str:
    return pref.locations[0].strip()


def build_query(
    template_id: str,
    pref: DiscoveryPreference,
    *,
    employer_name: str | None = None,
) -> BuiltQuery:
    role = _role(pref)
    location = _location(pref)
    if template_id == "CF1":
        return BuiltQuery(text=f"{role} {location}")
    if template_id == "CF1b":
        return BuiltQuery(text=f"{role} {location}")
    if template_id == "CF2":
        return BuiltQuery(text=f"{role} {location} hiring OR careers OR jobs")
    if template_id == "CF3":
        if not employer_name or not employer_name.strip():
            raise ValueError("CF3 requires employer_name")
        name = employer_name.strip()
        return BuiltQuery(text=f'"{name}" careers OR "official site" {location}')
    if template_id == "BD1":
        return BuiltQuery(
            text=f"{role} {location}",
            site=GREENHOUSE_SITE,
        )
    if template_id == "BD2":
        # One site param per Mojeek call; EU host post-filtered by extraction later.
        return BuiltQuery(
            text=f"{role} {location}",
            site=LEVER_SITES[0],
        )
    raise ValueError(f"unknown template_id: {template_id}")
