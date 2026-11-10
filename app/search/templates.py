"""Search template registry. Each template has one provider (D29, D49)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import SearchTemplate, SearchTemplateStatus

UNPROVEN_RUN_THRESHOLD = 5

# Google indexes boards on job-boards.greenhouse.io and the EU hosts too; the
# hit filter keeps any subdomain, and extraction only accepts real board hosts.
GREENHOUSE_SITE = "greenhouse.io"
LEVER_SITE = "jobs.lever.co"


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    id: str
    search_type: str
    pattern: str
    provider: str  # "reed" | "serpapi_jobs" | "serpapi"
    site: str | None = None
    needs_employer: bool = False


TEMPLATE_SPECS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        id="CF1",
        search_type="company-first",
        pattern="Reed API: role + location",
        provider="reed",
    ),
    TemplateSpec(
        id="CF1b",
        search_type="company-first",
        pattern="SerpAPI Google Jobs: role + location",
        provider="serpapi_jobs",
    ),
    TemplateSpec(
        id="CF2",
        search_type="company-first",
        pattern="role + location + hiring/careers",
        provider="serpapi",
    ),
    TemplateSpec(
        id="CF3",
        search_type="company-first",
        pattern="{employer} careers / official site",
        provider="serpapi",
        needs_employer=True,
    ),
    TemplateSpec(
        id="BD1",
        search_type="board-direct",
        pattern="Greenhouse boards: role + location (site filter)",
        provider="serpapi",
        site=GREENHOUSE_SITE,
    ),
    TemplateSpec(
        id="BD2",
        search_type="board-direct",
        # shortcut: Lever EU (jobs.eu.lever.co) needs a second run; add when V5 board signals land.
        pattern="Lever boards: role + location (site=jobs.lever.co)",
        provider="serpapi",
        site=LEVER_SITE,
    ),
)

_SPEC_BY_ID = {spec.id: spec for spec in TEMPLATE_SPECS}


def get_template_spec(template_id: str) -> TemplateSpec:
    try:
        return _SPEC_BY_ID[template_id]
    except KeyError as exc:
        raise ValueError(f"unknown template_id: {template_id}") from exc


def ensure_search_templates(session: Session) -> list[SearchTemplate]:
    """Upsert CF1–BD2 rows."""
    rows: list[SearchTemplate] = []
    for spec in TEMPLATE_SPECS:
        provider = spec.provider
        existing = session.get(SearchTemplate, spec.id)
        if existing is None:
            row = SearchTemplate(
                id=spec.id,
                search_type=spec.search_type,
                provider=provider,
                pattern=spec.pattern,
                status=SearchTemplateStatus.unproven,
            )
            session.add(row)
            rows.append(row)
        else:
            existing.search_type = spec.search_type
            existing.provider = provider
            existing.pattern = spec.pattern
            rows.append(existing)
    session.flush()
    return rows
