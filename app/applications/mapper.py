"""Map form field labels to application answers. Patterns first; unmatched required → needs_input."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.applicants.questions import EXTRA_BY_KEY, EXTRA_QUESTIONS
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
    # saved: the person's saved answers; bank: an answer they gave to this question on an earlier form;
    # ai: drafted from their CV and the advert and checked (D74), shown highlighted for them to review.
    source: str = "saved"
    note: str = ""


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
                    "source": s.source,
                    "note": s.note,
                }
                for s in self.steps
            ],
            "missing": self.missing,
        }


# Extra questions whose labels contain another one's words, so they are tried first.
_EXTRA_FIRST = ("gender_same_as_birth", "parents_university", "adjustments_details", "visa_expiry_date")
# An extra question that fills an answer the mapper already knows by another key.
_EXTRA_ALIAS = {"heard_about_source": "heard_about"}


def _extra_patterns() -> list[tuple[str, tuple[str, ...]]]:
    """The extra questions (D73), more specific than the generic names, address and gender patterns below."""
    ordered = sorted(EXTRA_QUESTIONS, key=lambda q: _EXTRA_FIRST.index(q.key) if q.key in _EXTRA_FIRST else len(_EXTRA_FIRST))
    return [(_EXTRA_ALIAS.get(q.key, q.key), q.patterns) for q in ordered if q.patterns]


# Longer / more specific patterns first. Short tokens use word boundaries.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    *_extra_patterns(),
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
    # 2021 Census wording, and the older wording answers were saved in before it.
    "female": ("woman", "f", "female including trans woman"),
    "male": ("man", "m", "male including trans man"),
    "woman": ("female",),
    "man": ("male",),
    "non binary": ("nonbinary",),
    "i describe myself another way": (
        "prefer to self describe", "self describe", "i use another term", "in another way", "other",
    ),
    "straight or heterosexual": ("heterosexual", "straight", "heterosexual or straight"),
    "gay or lesbian": ("lesbian or gay", "gay", "gay woman or lesbian"),
    "another sexual orientation": (
        "other sexual orientation", "another orientation", "other", "prefer to self describe", "i use another term",
    ),
}


def _norm(text: str) -> str:
    """Lower case, hyphens as spaces and "Gay/Lesbian" as "gay or lesbian"."""
    return re.sub(r"\s+", " ", re.sub(r"\s*/\s*", " or ", re.sub(r"[-_]", " ", text.lower()))).strip(" .")


def _loose(text: str) -> str:
    """Only the words that carry meaning, so "Asian/Asian British - Indian" matches "Asian or Asian British: Indian"."""
    return " ".join(word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in ("or", "and", "the"))


def _pick_ethnicity(options: list[str], desired: str) -> str | None:
    """A Census group ("White: Irish") as the form words it, else its broad group ("White")."""
    group, _, detail = desired.partition(": ")
    if not detail:
        return None
    first = group.split()[0].rstrip(",")
    wanted = [f"{group} {detail}", detail, f"{first} {detail}"]
    if detail.lower().startswith("any other"):
        wanted += [f"{first} other", f"other {first}", f"any other {first}"]
    if "British" in detail and first == "White":
        wanted += ["White British", "British", "White English Welsh Scottish Northern Irish British"]
    loose = {_loose(option): option for option in options}
    for text in wanted:
        if (found := loose.get(_loose(text))) is not None:
            return found
    return loose.get(_loose(group))


def _month_value(text: str, form_field: FormField) -> str | None:
    """A saved "YYYY-MM" as the form wants it: as is in a month box, "06/2027" typed, or a matching option."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match is None:
        return None
    year, month = match.group(1), int(match.group(2))
    if form_field.field_type == "month":
        return text
    if form_field.options:
        names = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December")
        for candidate in (f"{names[month - 1]} {year}", f"{names[month - 1][:3]} {year}", f"{month:02d}/{year}", year):
            if (found := _pick_option(form_field.options, candidate)) is not None:
                return found
        return None
    return f"{month:02d}/{year}"


def _date_value(text: str, form_field: FormField) -> str | None:
    """A saved ISO date: as is in a date box, the UK's day/month/year when typed."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match is None:
        return None
    if form_field.options:
        return _pick_option(form_field.options, text)
    if form_field.field_type == "date":
        return text
    return f"{match.group(3)}/{match.group(2)}/{match.group(1)}"


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
    question = EXTRA_BY_KEY.get(key)
    if question is not None and question.kind == "month":
        return _month_value(text, form_field)
    if question is not None and question.kind == "date":
        return _date_value(text, form_field)
    if form_field.options:
        picked = _pick_option(form_field.options, text)
        if picked is None and key == "ethnicity":
            picked = _pick_ethnicity(form_field.options, text)
        return picked
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
