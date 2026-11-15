"""Map form field labels to application answers. Patterns first; LLM only for leftovers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models import PREFER_NOT
from app.applications.schema import FormField, FormSchema

_YES_NO = {True: "Yes", False: "No", None: None}
_PREFER_LABELS = ("prefer not", "decline", "do not wish", "prefer not to say")


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


# (answer_key, label substrings). First match wins.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("legal_first_name", ("first name", "given name", "forename")),
    ("legal_last_name", ("last name", "surname", "family name")),
    ("full_name", ("full name", "your name", "candidate name")),
    ("email", ("email", "e-mail")),
    ("phone", ("phone", "mobile", "telephone", "tel")),
    ("address_line", ("street address", "address line", "home address", "address 1")),
    ("city", ("city", "town")),
    ("postcode", ("postcode", "postal code", "zip")),
    ("country", ("country",)),
    ("linkedin_url", ("linkedin",)),
    ("github_url", ("github",)),
    ("portfolio_url", ("portfolio", "personal website", "website", "personal url")),
    ("needs_visa_sponsorship", ("visa sponsorship", "require sponsorship", "need sponsorship", "sponsorship")),
    ("right_to_work_uk", ("right to work", "authorized to work", "authorised to work", "work authorization", "work authorisation", "legally authorized")),
    ("nationality", ("nationality", "citizenship")),
    ("previously_employed", ("previously employed", "worked for", "former employee", "previously worked")),
    ("heard_about", ("how did you hear", "how you heard", "where did you hear")),
    ("free_school_meals", ("free school meal", "free lunch", "school meals", "socio-economic", "socioeconomic")),
    ("gender", ("gender", "sex")),
    ("ethnicity", ("ethnicity", "race", "ethnic")),
    ("disability", ("disability", "disabled")),
    ("veteran", ("veteran", "military")),
    ("cv", ("resume", "cv", "curriculum vitae")),
]


def match_answer_key(label: str, name: str = "") -> str | None:
    hay = f"{label} {name}".lower()
    hay = re.sub(r"\s+", " ", hay)
    for key, needles in _PATTERNS:
        if any(needle in hay for needle in needles):
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
    for option in options:
        if lower in option.lower() or option.lower() in lower:
            return option
    return None


def _format_value(key: str, raw: object, field: FormField) -> str | None:
    if key == "cv":
        return "upload_cv"
    if isinstance(raw, bool):
        text = _YES_NO[raw]
    elif raw is None:
        return None
    else:
        text = str(raw)
    if text == PREFER_NOT and field.options:
        picked = _pick_option(field.options, PREFER_NOT)
        return picked
    if field.options:
        return _pick_option(field.options, text)
    if field.field_type in ("yes_no", "radio", "checkbox") and isinstance(raw, bool):
        return _YES_NO[raw]
    return text


def build_fill_plan(
    schema: FormSchema,
    answers: dict[str, object],
    *,
    llm_keys: dict[str, str] | None = None,
) -> FillPlan:
    """Deterministic mapping. ``llm_keys`` may supply answer_key for unmatched labels."""
    steps: list[FillStep] = []
    missing: list[str] = []
    llm_keys = llm_keys or {}
    for form_field in schema.fields:
        key = match_answer_key(form_field.label, form_field.name)
        if key is None:
            key = llm_keys.get(form_field.label) or llm_keys.get(form_field.name)
        if key is None or key == "ask_user":
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
        action = "select" if form_field.field_type in ("select", "radio") else "check" if form_field.field_type == "checkbox" else "fill"
        if form_field.field_type == "yes_no":
            action = "select"
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
