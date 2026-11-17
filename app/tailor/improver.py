"""Apply and check the AI's path-based CV edits.

Ported from Resume-Matcher's diff applier and verifier (srbhr/Resume-Matcher@959e458,
apps/backend/app/services/improver.py). The AI never returns a whole CV at this step. It returns
changes that each name a path, quote the text they replace and give the new text, and only the
gates below decide what lands.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Literal

Action = Literal["replace", "reorder"]


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    action: Action
    original: str | None
    value: str | list[str]
    reason: str


CHANGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "action": {"type": "string", "enum": ["replace", "reorder"]},
                    "original": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "value": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "reason": {"type": "string"},
                },
                "required": ["path", "action", "original", "value", "reason"],
            },
        },
        "strategy_notes": {"type": "string"},
    },
    "required": ["changes", "strategy_notes"],
}

_ALLOWED_PATHS = (
    re.compile(r"^summary$"),
    re.compile(r"^(?:workExperience|personalProjects)\[\d+\]\.description\[\d+\]$"),
    re.compile(r"^education\[\d+\]\.description$"),
)
_REORDERABLE = re.compile(r"^additional\.(?:technicalSkills|languages|certificationsTraining|awards)$")
_SEGMENT = re.compile(r"([a-zA-Z_]+)(?:\[(\d+)\])?")
_METRIC = re.compile(r"(?<!\d)\d+[%x]|[$£€]\d+")


def changes_from(payload: dict[str, Any]) -> list[Change]:
    return [
        Change(
            path=str(raw["path"]),
            action=raw["action"],
            original=raw["original"],
            value=raw["value"],
            reason=str(raw["reason"]),
        )
        for raw in payload.get("changes") or []
    ]


def apply_diffs(
    original: dict[str, Any], changes: list[Change]
) -> tuple[dict[str, Any], list[Change], list[Change]]:
    """(tailored CV, applied, rejected). A replace must quote the text it replaces."""
    result = copy.deepcopy(original)
    applied: list[Change] = []
    rejected: list[Change] = []
    for change in changes:
        spot = _locate(result, change.path)
        if change.action == "replace" and spot and any(p.match(change.path) for p in _ALLOWED_PATHS):
            parent, key = spot
            if (
                isinstance(parent[key], str)
                and isinstance(change.original, str)
                and parent[key].strip().casefold() == change.original.strip().casefold()
                and isinstance(change.value, str)
                and change.value.strip()
            ):
                parent[key] = change.value
                applied.append(change)
                continue
        if change.action == "reorder" and spot and _REORDERABLE.match(change.path) and isinstance(change.value, list):
            parent, key = spot
            parent[key] = _reorder(parent[key], change.value)
            applied.append(change)
            continue
        rejected.append(change)
    return result, applied, rejected


def verify_diff_result(original: dict[str, Any], result: dict[str, Any], applied: list[Change]) -> list[str]:
    """Warnings worth a look; none of them blocks the package."""
    if not applied:
        return ["No changes were applied; the CV is unchanged"]
    warnings = []
    before, after = _description_words(original), _description_words(result)
    if before and after > before * 1.8:
        warnings.append(f"Word count increased significantly: {before} to {after} ({after / before:.1f}x)")
    for change in applied:
        if change.action == "replace" and isinstance(change.value, str):
            invented = set(_METRIC.findall(change.value)) - set(_METRIC.findall(change.original or ""))
            if invented:
                warnings.append(f"Possible invented metric in {change.path}: {', '.join(sorted(invented))}")
    return warnings


def _locate(data: dict[str, Any], path: str) -> tuple[Any, str | int] | None:
    """The container holding the path's leaf, and the leaf's key or index."""
    spot: tuple[Any, str | int] | None = None
    current: Any = data
    for match in _SEGMENT.finditer(path):
        name, index = match.group(1), match.group(2)
        if not isinstance(current, dict) or name not in current:
            return None
        spot, current = (current, name), current[name]
        if index is not None:
            position = int(index)
            if not isinstance(current, list) or position >= len(current):
                return None
            spot, current = (current, position), current[position]
    return spot


def _reorder(items: list[str], proposed: list[str]) -> list[str]:
    """Originals in the proposed order, new items dropped, omitted originals kept at the end."""
    pool: dict[str, list[str]] = {}
    for item in items:
        pool.setdefault(item.casefold(), []).append(item)
    ordered = [pool[item.casefold()].pop(0) for item in proposed if pool.get(item.casefold())]
    return ordered + [pool[item.casefold()].pop(0) for item in items if pool[item.casefold()]]


def _description_words(data: dict[str, Any]) -> int:
    rows = [str(data.get("summary") or "")]
    for section in ("workExperience", "personalProjects"):
        for entry in data.get(section) or []:
            rows += map(str, entry.get("description") or [])
    return sum(len(row.split()) for row in rows)
