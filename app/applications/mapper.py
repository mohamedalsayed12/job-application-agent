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
    ("free_school_meals", (r"free school meal", r"free lunch", r"school meals")),
    # Before "socio-economic" questions in general: these are the two the Social Mobility Commission asks.
    ("parental_occupation", (r"household earner", r"parents?.{0,40}occupation", r"occupation.{0,40}parents?")),
    ("school_type", (r"type of school", r"school type", r"what school did you (mainly )?attend", r"kind of school")),
    ("free_school_meals", (r"socio-?economic",)),
    ("sexual_orientation", (r"sexual orientation", r"\bsexuality\b")),
    # Not "belief" or "faith" alone: declarations say "to the best of my knowledge and belief", "in good faith".
    ("religion", (r"\breligio",)),
    ("caring_responsibilities", (r"caring responsibilit", r"\bcarer\b", r"care for (a|an) (child|adult|relative)")),
    ("notice_period", (r"notice period",)),
    # Never a bare "start date": employment-history rows use that for past jobs.
    ("earliest_start_date", (r"earliest start", r"when (can|could) you start", r"available to start", r"availability to start", r"start date.{0,20}(available|earliest)")),
    ("willing_to_relocate", (r"\brelocat",)),
    ("work_hours", (r"full[- ]time or part[- ]time", r"part[- ]time or full[- ]time")),
    ("work_location", (r"remote,? hybrid", r"hybrid or remote", r"work remotely or", r"preferred work(ing)? (arrangement|location|model)")),
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


# Other wordings employers use for the fixed answers we save. Only whole-option matches count, so
# a near miss is still left for the applicant (needs_input) rather than guessed.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "available now": (
        "immediately", "immediate", "available immediately", "immediately available", "now",
        "no notice", "no notice period", "none", "0 weeks", "0 days",
    ),
    "1 week": ("one week", "7 days", "1 weeks"),
    "2 weeks": ("two weeks", "14 days", "a fortnight", "fortnight"),
    "1 month": ("one month", "4 weeks", "four weeks", "30 days", "1 months"),
    "2 months": ("two months", "8 weeks", "eight weeks", "60 days"),
    "3 months or more": (
        "3 months", "three months", "3+ months", "3 months+", "more than 3 months",
        "3 months or longer", "12 weeks", "90 days",
    ),
    "full time": ("fulltime", "full time only"),
    "part time": ("parttime", "part time only"),
    "either": ("both", "no preference", "full time or part time", "part time or full time", "open to both"),
    "on site": ("onsite", "in office", "office based", "in person", "office"),
    "hybrid": ("hybrid working", "flexible hybrid"),
    "remote": ("fully remote", "remote only", "work from home", "home based"),
    "any": ("no preference", "any", "flexible", "open to all", "open to any"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_]", " ", text.lower())).strip(" .")


def _pick_option(options: list[str], desired: str) -> str | None:
    if not options:
        return desired
    lower = desired.lower()
    for option in options:
        if option.lower() == lower:
            return option
    wanted = {_norm(desired), *_SYNONYMS.get(_norm(desired), ())}
    for option in options:
        if _norm(option) in wanted:
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
