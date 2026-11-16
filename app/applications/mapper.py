"""Map form field labels to application answers. Patterns first; unmatched required → needs_input."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.applications.schema import FormField, FormSchema
from app.models import PREFER_NOT

_YES_NO = {True: "Yes", False: "No", None: None}
_PREFER_LABELS = ("prefer not", "decline", "do not wish", "prefer not to say", "do not want to answer", "rather not")


@dataclass(frozen=True, slots=True)
class FillStep:
    label: str
    name: str
    action: str  # fill | select | check | upload_cv
    value: str
    required: bool


@dataclass(frozen=True, slots=True)
class FillPlan:
    steps: list[FillStep]
    missing: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "label": s.label,
                    "name": s.name,
                    "action": s.action,
                    "value": s.value,
                    "required": s.required,
                }
                for s in self.steps
            ],
            "missing": self.missing,
        }


# Longer / more specific patterns first. Short tokens use word boundaries.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("needs_visa_sponsorship", (r"visa sponsorship", r"require sponsorship", r"need sponsorship", r"\bsponsorship\b")),
    ("right_to_work_uk", (r"right to work", r"authorized to work", r"authorised to work", r"work authorization", r"work authorisation", r"legally authorized")),
    ("previously_employed", (r"previously employed", r"worked for .{0,20}(us|company|organisation|organization)", r"former employee", r"previously worked")),
    ("heard_about", (r"how did you hear", r"how you heard", r"where did you hear")),
    ("free_school_meals", (r"free school meal", r"free lunch", r"school meals", r"socio-?economic")),
    ("linkedin_url", (r"\blinkedin\b",)),
    ("github_url", (r"\bgithub\b",)),
    ("portfolio_url", (r"\bportfolio\b", r"personal website", r"personal url")),
    ("legal_first_name", (r"first name", r"given name", r"forename")),
    ("legal_last_name", (r"last name", r"surname", r"family name")),
    ("full_name", (r"full name", r"your name", r"candidate name")),
    ("email", (r"\be-?mail\b",)),
    ("phone", (r"\bphone\b", r"\bmobile\b", r"\btelephone\b", r"\btel\b")),
    ("address_line", (r"street address", r"address line", r"home address", r"address 1")),
    ("city", (r"\bcity\b", r"\btown\b")),
    ("postcode", (r"postcode", r"postal code", r"\bzip\b")),
    ("country", (r"\bcountry\b",)),
    ("nationality", (r"\bnationality\b", r"\bcitizenship\b")),
    ("gender", (r"\bgender\b", r"(?<!orientation )\bsex\b(?!ual)")),
    ("ethnicity", (r"\bethnicity\b", r"\brace\b", r"\bethnic\b")),
    ("disability", (r"\bdisability\b", r"\bdisabled\b")),
    ("veteran", (r"\bveteran\b", r"\bmilitary\b")),
    ("cv", (r"\bresume\b", r"\bcv\b", r"curriculum vitae")),
]


def match_answer_key(label: str, name: str = "") -> str | None:
    hay = re.sub(r"\s+", " ", f"{label} {name}".lower())
    for key, needles in _PATTERNS:
        if any(re.search(needle, hay) for needle in needles):
            return key
    return None


def _pick_option(options: list[str], desired: str) -> str | None:
    if not options:
        return desired
    lower = desired.lower()
    for option in options:
        if option.lower() == lower:
            return option
    if any(p in lower for p in _PREFER_LABELS) or desired == PREFER_NOT:
        for option in options:
            ol = option.lower()
            if any(p in ol for p in _PREFER_LABELS):
                return option
        return None
    return None


def _format_value(key: str, raw: object, form_field: FormField) -> str | None:
    if key == "cv":
        return "upload_cv"
    if isinstance(raw, bool):
        text = _YES_NO[raw]
    elif raw is None:
        return None
    else:
        text = str(raw)
    if text == PREFER_NOT and form_field.options:
        return _pick_option(form_field.options, PREFER_NOT)
    if form_field.options:
        return _pick_option(form_field.options, text)
    if form_field.field_type in ("yes_no", "radio", "checkbox") and isinstance(raw, bool):
        return _YES_NO[raw]
    return text


def build_fill_plan(schema: FormSchema, answers: dict[str, object]) -> FillPlan:
    """Deterministic mapping. Unmatched required labels become missing (needs_input)."""
    steps: list[FillStep] = []
    missing: list[str] = []
    for form_field in schema.fields:
        key = match_answer_key(form_field.label, form_field.name)
        if key is None:
            if form_field.required:
                missing.append(form_field.label or form_field.name)
            continue
        raw = answers.get(key)
        if key == "cv":
            steps.append(
                FillStep(
                    label=form_field.label,
                    name=form_field.name,
                    action="upload_cv",
                    value="cv",
                    required=form_field.required,
                )
            )
            continue
        value = _format_value(key, raw, form_field)
        if value is None or value == "":
            if form_field.required:
                missing.append(form_field.label or form_field.name)
            continue
        action = (
            "select"
            if form_field.field_type in ("select", "radio", "yes_no")
            else "check"
            if form_field.field_type == "checkbox"
            else "fill"
        )
        steps.append(
            FillStep(
                label=form_field.label,
                name=form_field.name,
                action=action,
                value=value,
                required=form_field.required,
            )
        )
    return FillPlan(steps=steps, missing=missing)


def plan_from_json(data: dict[str, Any]) -> FillPlan:
    return FillPlan(
        steps=[
            FillStep(
                label=str(s.get("label") or ""),
                name=str(s.get("name") or ""),
                action=str(s.get("action") or "fill"),
                value=str(s.get("value") or ""),
                required=bool(s.get("required")),
            )
            for s in (data.get("steps") or [])
            if isinstance(s, dict)
        ],
        missing=[str(m) for m in (data.get("missing") or [])],
    )
