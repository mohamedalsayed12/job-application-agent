"""Curated outreach success patterns (versioned JSON, not live Reddit scrapes)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.models import OutreachRoleKind

_DATA = Path(__file__).with_name("data") / "patterns.json"

@dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    label: str
    rule: str
    intents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatternLibrary:
    version: str
    patterns: tuple[Pattern, ...]

    def for_role(self, role_kind: str) -> list[Pattern]:
        intent = intent_for(role_kind)
        return [p for p in self.patterns if intent in p.intents]


@lru_cache(maxsize=1)
def load_patterns() -> PatternLibrary:
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    patterns = tuple(
        Pattern(
            id=str(row["id"]),
            label=str(row["label"]),
            rule=str(row["rule"]),
            intents=tuple(str(i) for i in row.get("intents", ())),
        )
        for row in raw["patterns"]
    )
    return PatternLibrary(version=str(raw["version"]), patterns=patterns)


def intent_for(role_kind: str) -> str:
    """Pattern-library intent for a contact role (matches patterns.json intents)."""
    try:
        kind = OutreachRoleKind(role_kind)
    except ValueError:
        return "hiring_manager"
    if kind == OutreachRoleKind.unknown:
        return "hiring_manager"
    return kind.value
