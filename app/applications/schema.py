"""Classify apply URLs and normalise form field schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.models import AtsKind


@dataclass(frozen=True, slots=True)
class FormField:
    label: str
    name: str
    field_type: str  # text, textarea, select, radio, checkbox, file, yes_no, date, month
    required: bool
    options: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FormSchema:
    ats_kind: AtsKind
    fields: list[FormField]
    source: str  # extension: read from the page in the person's browser


def classify_ats(apply_url: str) -> AtsKind:
    host = urlparse(apply_url).netloc.lower()
    if "greenhouse.io" in host or host.endswith("grnh.se"):
        return AtsKind.greenhouse
    if "lever.co" in host:
        return AtsKind.lever
    return AtsKind.unknown


# Field types the extension reports, mapped onto the mapper's types.
_SCAN_TYPES = {
    "text": "text", "email": "text", "tel": "text", "url": "text", "number": "text", "date": "date", "month": "month",
    "search": "text", "textarea": "textarea", "select": "select", "select-one": "select",
    "select-multiple": "select", "radio": "radio", "checkbox": "checkbox", "file": "file",
    # Custom dropdowns (Workday, Greenhouse, Ashby): their options are only read once opened (D74).
    "combobox": "select",
}


def fields_from_scan(items: list[dict[str, Any]], *, ats: AtsKind) -> FormSchema:
    """The form as the extension read it in the person's own browser."""
    fields: list[FormField] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        name = str(item.get("name") or "").strip()
        if not (label or name):
            continue
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        fields.append(
            FormField(
                label=label or name,
                name=name or label,
                field_type=_SCAN_TYPES.get(str(item.get("type") or "text").lower(), "text"),
                required=bool(item.get("required")),
                options=options,
            )
        )
    return FormSchema(ats_kind=ats, fields=fields, source="extension")


def schema_to_json(schema: FormSchema) -> dict[str, Any]:
    return {
        "ats_kind": schema.ats_kind.value,
        "source": schema.source,
        "fields": [
            {
                "label": f.label,
                "name": f.name,
                "type": f.field_type,
                "required": f.required,
                "options": f.options,
            }
            for f in schema.fields
        ],
    }
