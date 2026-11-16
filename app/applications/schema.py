"""Classify apply URLs and normalise form field schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.models import AtsKind


@dataclass(frozen=True, slots=True)
class FormField:
    label: str
    name: str
    field_type: str  # text, textarea, select, radio, checkbox, file, yes_no
    required: bool
    options: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FormSchema:
    ats_kind: AtsKind
    fields: list[FormField]
    source: str  # greenhouse_api | apify | stub


def classify_ats(apply_url: str) -> AtsKind:
    host = urlparse(apply_url).netloc.lower()
    if "greenhouse.io" in host or host.endswith("grnh.se"):
        return AtsKind.greenhouse
    if "lever.co" in host:
        return AtsKind.lever
    return AtsKind.unknown


def parse_greenhouse_board(apply_url: str) -> tuple[str, str] | None:
    """Return (board_token, job_id) when the URL names a Greenhouse board job."""
    parsed = urlparse(apply_url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    query = parse_qs(parsed.query)

    if "boards.greenhouse.io" in host or "job-boards.greenhouse.io" in host:
        if "for" in query and ("token" in query or "gh_jid" in query):
            board = query["for"][0]
            job = (query.get("token") or query.get("gh_jid") or [""])[0]
            if board and job:
                return board, job
        if len(parts) >= 3 and parts[1] == "jobs":
            return parts[0], parts[2]
        if len(parts) >= 2 and parts[0] and parts[1].isdigit():
            return parts[0], parts[1]

    if "embed" in path and "for" in query:
        board = query["for"][0]
        job = (query.get("token") or query.get("gh_jid") or [""])[0]
        if board and job:
            return board, job
    return None


def greenhouse_questions_to_schema(payload: dict[str, Any]) -> FormSchema:
    fields: list[FormField] = [
        FormField("First name", "first_name", "text", True),
        FormField("Last name", "last_name", "text", True),
        FormField("Email", "email", "text", True),
        FormField("Phone", "phone", "text", False),
        FormField("Resume", "resume", "file", True),
    ]
    for question in payload.get("questions") or []:
        if not isinstance(question, dict):
            continue
        label = str(question.get("label") or question.get("name") or "").strip()
        name = str(question.get("name") or label).strip()
        if not label:
            continue
        qtype = str(question.get("type") or "input_text").lower()
        field_type = {
            "input_file": "file",
            "textarea": "textarea",
            "multi_value_multi_select": "select",
            "multi_value_single_select": "select",
            "single_select": "select",
            "multi_select": "select",
            "boolean": "yes_no",
        }.get(qtype, "text")
        options = []
        for value in question.get("values") or []:
            if isinstance(value, dict) and value.get("label"):
                options.append(str(value["label"]))
            elif isinstance(value, str):
                options.append(value)
        fields.append(
            FormField(
                label=label,
                name=name,
                field_type=field_type,
                required=bool(question.get("required")),
                options=options,
            )
        )
    for question in payload.get("demographic_questions") or []:
        if not isinstance(question, dict):
            continue
        label = str(question.get("label") or "").strip()
        name = str(question.get("name") or label).strip()
        if not label:
            continue
        options = [str(v.get("label") if isinstance(v, dict) else v) for v in (question.get("values") or [])]
        fields.append(FormField(label=label, name=name, field_type="select", required=False, options=options))
    return FormSchema(ats_kind=AtsKind.greenhouse, fields=fields, source="greenhouse_api")


def fields_from_apify_items(items: list[dict[str, Any]], *, ats: AtsKind) -> FormSchema:
    fields: list[FormField] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        name = str(item.get("name") or label).strip()
        if not label:
            continue
        options = [str(o) for o in (item.get("options") or []) if o]
        fields.append(
            FormField(
                label=label,
                name=name,
                field_type=str(item.get("type") or "text"),
                required=bool(item.get("required")),
                options=options,
            )
        )
    return FormSchema(ats_kind=ats, fields=fields, source="apify")


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


def schema_from_json(data: dict[str, Any]) -> FormSchema:
    return FormSchema(
        ats_kind=AtsKind(data.get("ats_kind") or "unknown"),
        source=str(data.get("source") or "stub"),
        fields=[
            FormField(
                label=str(f.get("label") or ""),
                name=str(f.get("name") or ""),
                field_type=str(f.get("type") or "text"),
                required=bool(f.get("required")),
                options=[str(o) for o in (f.get("options") or [])],
            )
            for f in (data.get("fields") or [])
            if isinstance(f, dict)
        ],
    )
