"""Search template registry. Provider names come from settings (D29)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import SearchTemplate, SearchTemplateStatus

# CF1b stays seeded but disabled until V9 (D29).
DISABLED_UNTIL_V9 = frozenset({"CF1b"})

UNPROVEN_RUN_THRESHOLD = 5


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    id: str
    search_type: str
    pattern: str
    provider_kind: str  # "reed" | "adzuna" | "web_search"


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
    ),
    TemplateSpec(
        id="BD1",
        search_type="board-direct",
        pattern="Greenhouse boards: role + location (site filter)",
        provider_kind="web_search",
    ),
    TemplateSpec(
        id="BD2",
        search_type="board-direct",
        pattern="Lever boards global/EU: role + location (site filter)",
        provider_kind="web_search",
    ),
)

ENABLED_TEMPLATE_IDS = frozenset(
    spec.id for spec in TEMPLATE_SPECS if spec.id not in DISABLED_UNTIL_V9
)


def resolve_provider(spec: TemplateSpec, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    if spec.provider_kind == "reed":
        return "reed"
    if spec.provider_kind == "adzuna":
        return "adzuna"
    return cfg.web_search_provider


def is_template_enabled(template_id: str, settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    if template_id == "CF1b":
        return cfg.adzuna_enabled
    return template_id in ENABLED_TEMPLATE_IDS


def ensure_search_templates(
    session: Session,
    settings: Settings | None = None,
) -> list[SearchTemplate]:
    """Upsert CF1–BD2 rows. CF1b stays `unproven` and disabled until V9."""
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
