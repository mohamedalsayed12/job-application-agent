"""Form pieces for employer-application answers, shared by the sign-up wizard and the answers page."""

from __future__ import annotations

import html
from dataclasses import replace

from starlette.datastructures import FormData

from app.applicants.apply_answers import (
    ANSWER_FIELDS,
    DIVERSITY_CHOICES,
    NOTICE_PERIODS,
    WORK_HOURS,
    WORK_LOCATIONS,
    AnswersInput,
)
from app.models import PREFER_NOT

DIVERSITY_LABELS = {
    "gender": "Gender",
    "ethnicity": "Ethnic group",
    "disability": "Do you consider yourself to have a disability or long-term health condition?",
    "veteran": "Have you served in the armed forces?",
    "free_school_meals": "Were you eligible for free school meals at any point during your school years?",
    "sexual_orientation": "Sexual orientation",
    "religion": "Religion or belief",
    "caring_responsibilities": "Are you a primary carer for a child, or for an adult who needs care?",
    "school_type": "What type of school did you mainly attend between the ages of 11 and 16?",
    "parental_occupation": (
        "What was the occupation of your main household earner when you were about 14?"
    ),
}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _label(value: str) -> str:
    return "Prefer not to say" if value == PREFER_NOT else value


def choice_select(name: str, choices: tuple[str, ...], current: str, *, blank: str | None = None, required: bool = False) -> str:
    """A saved answer that isn't one of the choices (typed before they existed) stays selectable."""
    options = ([("", blank)] if blank is not None else []) + [(choice, _label(choice)) for choice in choices]
    if current and current not in choices:
        options.append((current, current))
    body = "".join(
        f'<option value="{esc(value)}"{" selected" if value == current else ""}>{esc(text)}</option>'
        for value, text in options
    )
    return f'<select name="{name}" id="{name}"{" required" if required else ""}>{body}</select>'


def yes_no_select(name: str, current: str, *, required: bool = False) -> str:
    """`current` is "yes", "no" or "unset". A required one has no preset answer, so nothing is assumed."""
    chosen = current if current in ("yes", "no") else ""
    options = (("", "Choose one" if required else "Not set"), ("yes", "Yes"), ("no", "No"))
    body = "".join(
        f'<option value="{value}"{" selected" if value == chosen else ""}>{text}</option>' for value, text in options
    )
    return f'<select name="{name}" id="{name}"{" required" if required else ""}>{body}</select>'


def availability_fields(answers: AnswersInput, *, required: bool) -> str:
    return f"""
<div class="row">
  <label>Notice period {choice_select("notice_period", NOTICE_PERIODS, answers.notice_period, blank="Choose one", required=required)}</label>
  <label>Earliest start date <span class="hint">optional</span>
    <input name="earliest_start_date" type="date" value="{esc(answers.earliest_start_date)}"/>
  </label>
</div>
<div class="row">
  <label>Full-time or part-time {choice_select("work_hours", WORK_HOURS, answers.work_hours, blank="Choose one", required=required)}</label>
  <label>Where you want to work {choice_select("work_location", WORK_LOCATIONS, answers.work_location, blank="Choose one", required=required)}</label>
</div>
<label>Willing to relocate for a job {yes_no_select("willing_to_relocate", answers.willing_to_relocate, required=required)}</label>
"""


def diversity_fields(answers: AnswersInput) -> str:
    return "".join(
        f'<label>{esc(DIVERSITY_LABELS[key])} {choice_select(key, choices, getattr(answers, key) or PREFER_NOT)}</label>'
        for key, choices in DIVERSITY_CHOICES.items()
    )


def answers_from_form(form: FormData, base: AnswersInput, names: tuple[str, ...]) -> AnswersInput:
    """`base` with the named answers replaced by what was sent. Answers a page doesn't show are kept."""
    sent = {name: str(form.get(name) or "") for name in names if name in ANSWER_FIELDS}
    return replace(base, **sent)
