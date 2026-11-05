"""Search template registry. Provider names come from settings (D29)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import SearchTemplate, SearchTemplateStatus

UNPROVEN_RUN_THRESHOLD = 5

GREENHOUSE_SITE = "boards.greenhouse.io"
LEVER_SITE = "jobs.lever.co"


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    id: str
    search_type: str
    pattern: str
    provider_kind: str  # "reed" | "adzuna" | "web_search"
    site: str | None = None
    needs_employer: bool = False


TEMPLATE_SPECS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        id="CF1",
        search_type="company-first",
        pattern="Reed API: role + location",
        provider_kind="reed",
    ),
    TemplateSpec(
        id="CF1b",
        search_type="company-first",
        pattern="Adzuna API: role + location (off until V9)",
        provider_kind="adzuna",
    ),
    TemplateSpec(
        id="CF2",
        search_type="company-first",
        pattern="role + location + hiring/careers",
        provider_kind="web_search",
    ),
    TemplateSpec(
        id="CF3",
        search_type="company-first",
        pattern="{employer} careers / official site",
        provider_kind="web_search",
        needs_employer=True,
    ),
    TemplateSpec(
        id="BD1",
        search_type="board-direct",
        pattern="Greenhouse boards: role + location (site filter)",
        provider_kind="web_search",
        site=GREENHOUSE_SITE,
    ),
    TemplateSpec(
        id="BD2",
        search_type="board-direct",
        # shortcut: Lever EU (jobs.eu.lever.co) needs a second run; add when V5 board signals land.
        pattern="Lever boards: role + location (site=jobs.lever.co)",
        provider_kind="web_search",
        site=LEVER_SITE,
    ),
)

_SPEC_BY_ID = {spec.id: spec for spec in TEMPLATE_SPECS}


def get_template_spec(template_id: str) -> TemplateSpec:
    try:
        return _SPEC_BY_ID[template_id]
    except KeyError as exc:
        raise ValueError(f"unknown template_id: {template_id}") from exc


def resolve_provider(spec: TemplateSpec, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    if spec.provider_kind == "reed":
        return "reed"
    if spec.provider_kind == "adzuna":
        return "adzuna"
    return cfg.web_search_provider


def is_template_enabled(template_id: str, settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    if template_id not in _SPEC_BY_ID:
        return False
    if template_id == "CF1b":
        return cfg.adzuna_enabled
    return True


def ensure_search_templates(
    session: Session,
    settings: Settings | None = None,
) -> list[SearchTemplate]:
    """Upsert CF1–BD2 rows. CF1b stays seeded and disabled until V9."""
    cfg = settings or get_settings()
    rows: list[SearchTemplate] = []
    for spec in TEMPLATE_SPECS:
        provider = resolve_provider(spec, cfg)
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
