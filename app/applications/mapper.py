"""Match each question on an application form to something the person already gave.

A value comes from their apply answers, an answer they saved for an earlier form, their account email,
or text copied from their CV. Nothing else is ever filled. A required question none of those answers is
returned as missing, so the person can answer it once and have it saved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.applicants.apply_answers import SavedQuestion, question_key
from app.models import PREFER_NOT

FieldType = Literal["text", "email", "tel", "url", "number", "date", "textarea", "select", "radio", "checkbox", "file"]
Action = Literal["fill", "select", "check", "upload_cv"]
Source = Literal["answers", "saved", "account", "cv"]

MAX_QUOTE = 200
_PREFER_LABELS = ("prefer not", "decline", "do not wish", "do not want to answer", "rather not")


class FormField(BaseModel):
    """One question the extension found on the employer's page."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=300)
    type: FieldType
    required: bool = False
    options: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=400)

    @property
    def question(self) -> str:
        return self.label or self.name


@dataclass(frozen=True, slots=True)
class Fact:
    """Something the person gave, with the question it answers."""

    id: str
    question: str
    value: str
    source: Source


@dataclass(frozen=True, slots=True)
class Match:
    """What the model pointed at for one field: one of the person's facts, or a quote from their CV."""

    fact_id: str = ""
    cv_quote: str = ""


@dataclass(frozen=True, slots=True)
class FillStep:
    field_id: str
    action: Action
    value: str
    source: Source


@dataclass(frozen=True, slots=True)
class Question:
    """A required question nothing the person gave answers yet."""

    field_id: str
    label: str
    type: FieldType
    options: list[str]


@dataclass(frozen=True, slots=True)
class FillPlan:
    steps: list[FillStep]
    missing: list[Question]
    # Required uploads other than the CV, such as a cover letter, which the person attaches themselves.
    attach: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "steps": [asdict(step) for step in self.steps],
            "missing": [asdict(question) for question in self.missing],
            "attach": self.attach,
        }


# Longer, more specific patterns first. Short tokens use word boundaries.
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


# Other wordings employers use for the fixed answers we save. Only whole-option matches count.
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


def answer_fact_id(key: str) -> str:
    return "account:email" if key == "email" else f"answers:{key}"


def facts_from_profile(answers: Mapping[str, object], saved: list[SavedQuestion]) -> list[Fact]:
    facts: list[Fact] = []
    for key, raw in answers.items():
        if raw is None or key == "cv" or raw == "":
            continue
        value = {True: "Yes", False: "No"}.get(raw, str(raw))
        source: Source = "account" if key == "email" else "answers"
        facts.append(Fact(answer_fact_id(key), key.replace("_", " "), value, source))
    for item in saved:
        cleaned_question, cleaned_answer = item.question.strip(), item.answer.strip()
        if cleaned_question and cleaned_answer:
            facts.append(Fact(f"saved:{question_key(cleaned_question)}", cleaned_question, cleaned_answer, "saved"))
    return facts


def matches_from_payload(payload: Mapping[str, object]) -> dict[str, Match]:
    """Model output may only point at a fact or a CV quote. Any other key is ignored."""
    raw = payload.get("matches")
    if not isinstance(raw, list):
        return {}
    found: dict[str, Match] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        field_id = item.get("field_id")
        if not isinstance(field_id, str) or not field_id:
            continue
        fact_id = item.get("fact_id")
        quote = item.get("cv_quote")
        found[field_id] = Match(
            fact_id=fact_id if isinstance(fact_id, str) else "",
            cv_quote=quote if isinstance(quote, str) else "",
        )
    return found


def _norm(text: str) -> str:
    cleaned = re.sub(r"[-_]", " ", text.casefold())
    return " ".join(cleaned.split()).strip(" .:*?")


def _pick_option(options: list[str], desired: str) -> str | None:
    """One option that says the same as the person's answer, or None when none or several do."""
    want = _norm(desired)
    if desired == PREFER_NOT or any(phrase in want for phrase in _PREFER_LABELS):
        hits = [option for option in options if any(phrase in _norm(option) for phrase in _PREFER_LABELS)]
        return hits[0] if len(hits) == 1 else None
    exact = [option for option in options if _norm(option) == want]
    if exact:
        return exact[0]
    wanted = {want, *_SYNONYMS.get(want, ())}
    syn_hits = [option for option in options if _norm(option) in wanted]
    if len(syn_hits) == 1:
        return syn_hits[0]
    hits = [option for option in options if re.match(rf"{re.escape(want)}\b", _norm(option))]
    return hits[0] if len(hits) == 1 else None


def _fit(form_field: FormField, value: str) -> str | None:
    """The value as this field takes it, or None when the field cannot take it."""
    if form_field.options:
        return _pick_option(form_field.options, value)
    if form_field.type == "checkbox":
        return {"yes": "Yes", "no": "No"}.get(_norm(value))
    if value == PREFER_NOT:
        return "Prefer not to say"
    return value or None


def _known(form_field: FormField, facts: Mapping[str, Fact], saved: Mapping[str, Fact]) -> tuple[str, Source] | None:
    key = match_answer_key(form_field.label, form_field.name)
    candidates = [facts.get(answer_fact_id(key))] if key and key != "cv" else []
    candidates.append(saved.get(question_key(form_field.question)))
    for fact in candidates:
        if fact is not None and (value := _fit(form_field, fact.value)):
            return value, fact.source
    return None


def _cv_span(quote: str, cv_text: str) -> str | None:
    """The quote as the CV writes it, when it is its own span and not letters inside a longer word."""
    quote = " ".join(quote.split())
    if not quote or len(quote) > MAX_QUOTE:
        return None
    hay = " ".join(cv_text.split())
    found = re.search(re.escape(quote), hay, re.IGNORECASE)
    if found is None:
        return None
    before = hay[found.start() - 1] if found.start() else ""
    after = hay[found.end()] if found.end() < len(hay) else ""
    if (before and before.isalnum()) or (after and after.isalnum()):
        return None
    return found.group(0)


def _grounded(form_field: FormField, match: Match | None, facts: Mapping[str, Fact], cv_text: str) -> tuple[str, Source] | None:
    """Keep what the model returned only when it is the person's own fact or CV text that fits the field."""
    if match is None:
        return None
    fact = facts.get(match.fact_id)
    if fact is not None and (value := _fit(form_field, fact.value)):
        return value, fact.source
    quote = _cv_span(match.cv_quote, cv_text)
    if quote is not None and (value := _fit(form_field, quote)):
        return value, "cv"
    return None


def _indexes(facts: list[Fact]) -> tuple[dict[str, Fact], dict[str, Fact]]:
    by_id = {fact.id: fact for fact in facts}
    saved = {question_key(fact.question): fact for fact in facts if fact.source == "saved"}
    return by_id, saved


def _action(form_field: FormField) -> Action:
    if form_field.type in ("select", "radio"):
        return "select"
    return "check" if form_field.type == "checkbox" else "fill"


def unmatched(fields: list[FormField], facts: list[Fact]) -> list[FormField]:
    """Fields the patterns and saved answers leave open: the ones worth asking the model about."""
    by_id, saved = _indexes(facts)
    return [f for f in fields if f.type != "file" and _known(f, by_id, saved) is None]


def build_fill_plan(
    fields: list[FormField],
    facts: list[Fact],
    *,
    cv_text: str = "",
    matches: Mapping[str, Match] | None = None,
) -> FillPlan:
    by_id, saved = _indexes(facts)
    matches = matches or {}
    steps: list[FillStep] = []
    missing: list[Question] = []
    attach: list[str] = []
    for form_field in fields:
        if form_field.type == "file":
            if match_answer_key(form_field.label, form_field.name) == "cv":
                steps.append(FillStep(form_field.id, "upload_cv", "cv", "cv"))
            elif form_field.required:
                attach.append(form_field.question)
            continue
        found = _known(form_field, by_id, saved) or _grounded(form_field, matches.get(form_field.id), by_id, cv_text)
        if found is not None:
            steps.append(FillStep(form_field.id, _action(form_field), *found))
        elif form_field.required:
            missing.append(Question(form_field.id, form_field.question, form_field.type, list(form_field.options)))
    return FillPlan(steps=steps, missing=missing, attach=attach)
