"""Load and save employer-application answers (separate from the job-search profile)."""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field, fields
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.questions import EXTRA_BY_KEY, EXTRA_QUESTIONS
from app.models import PREFER_NOT, ApplicationAnswers

MAX_FIELD = 200
# Extra questions such as A-level grades or the adjustments someone needs can run longer.
MAX_EXTRA_TEXT = 500
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
MAX_URL = 500
MAX_ANSWER = 2000
MAX_SAVED = 40

# Choices the forms offer. A stored answer outside these lists (typed before they existed) still shows.
NOTICE_PERIODS = ("Available now", "1 week", "2 weeks", "1 month", "2 months", "3 months or more")
WORK_HOURS = ("Full-time", "Part-time", "Either")
WORK_LOCATIONS = ("On-site", "Hybrid", "Remote", "Any")

# Diversity monitoring questions UK employers ask. Each list ends with prefer not to say, the default.
# Gender, ethnic group, sexual orientation and religion follow the 2021 Census wording (D74); school type
# and parental occupation follow the Social Mobility Commission's.
GENDERS = ("Female", "Male", "Non-binary", "I describe myself another way", PREFER_NOT)
# The Census's 19 groups, each as "Broad group: group" so a form offering only the broad groups still matches.
ETHNIC_GROUPS = (
    "Asian or Asian British: Indian",
    "Asian or Asian British: Pakistani",
    "Asian or Asian British: Bangladeshi",
    "Asian or Asian British: Chinese",
    "Asian or Asian British: Any other Asian background",
    "Black, Black British, Caribbean or African: Caribbean",
    "Black, Black British, Caribbean or African: African",
    "Black, Black British, Caribbean or African: Any other Black, Black British or Caribbean background",
    "Mixed or multiple ethnic groups: White and Black Caribbean",
    "Mixed or multiple ethnic groups: White and Black African",
    "Mixed or multiple ethnic groups: White and Asian",
    "Mixed or multiple ethnic groups: Any other Mixed or multiple ethnic background",
    "White: English, Welsh, Scottish, Northern Irish or British",
    "White: Irish",
    "White: Gypsy or Irish Traveller",
    "White: Roma",
    "White: Any other White background",
    "Other ethnic group: Arab",
    "Other ethnic group: Any other ethnic group",
)
DIVERSITY_CHOICES: dict[str, tuple[str, ...]] = {
    "gender": GENDERS,
    "ethnicity": (*ETHNIC_GROUPS, PREFER_NOT),
    "disability": ("Yes", "No", PREFER_NOT),
    "veteran": ("Yes", "No", PREFER_NOT),
    "free_school_meals": ("Yes", "No", "Not applicable (finished school outside the UK)", "I don't know", PREFER_NOT),
    "sexual_orientation": ("Straight or heterosexual", "Gay or lesbian", "Bisexual", "Another sexual orientation", PREFER_NOT),
    "religion": (
        "No religion",
        "Buddhist",
        "Christian",
        "Hindu",
        "Jewish",
        "Muslim",
        "Sikh",
        "Another religion or belief",
        PREFER_NOT,
    ),
    "caring_responsibilities": ("Yes", "No", PREFER_NOT),
    "school_type": (
        "A state-run or state-funded school",
        "A state-run or state-funded school that selected on academic, faith or other grounds",
        "An independent or fee-paying school",
        "An independent or fee-paying school, with a bursary covering 90% or more of the fees",
        "A school outside the UK",
        "I don't know",
        PREFER_NOT,
    ),
    "parental_occupation": (
        "Modern or traditional professional, such as teacher, nurse, engineer, doctor or lawyer",
        "Senior, middle or junior manager or administrator",
        "Clerical or intermediate, such as secretary, call centre agent or nursery nurse",
        "Technical or craft, such as electrician, plumber or printer",
        "Routine, semi-routine manual or service, such as driver, cleaner, porter or care worker",
        "Small business owner with fewer than 25 employees",
        "Long-term unemployed (claimed benefits for more than a year)",
        "Other, such as retired, or this question doesn't apply to me",
        "I don't know",
        PREFER_NOT,
    ),
}
_DEMO_KEYS = tuple(DIVERSITY_CHOICES)
# shortcut: two different questions that keep the same content words share a key, upgrade when a saved answer fills the wrong question.
_STOP_WORDS = frozenset(
    "a about an and are as at be briefly by can could describe do does explain for from has have how i if in is it "
    "kindly me my of on or our please s tell the this to us we what when where which who will with would you your".split()
)


@dataclass(frozen=True, slots=True)
class SavedQuestion:
    question: str
    answer: str


def question_key(text: str) -> str:
    """The same question in other words, punctuation, or order gets the same key."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    content = sorted({word for word in words if word not in _STOP_WORDS})
    return " ".join(content or words)


def _saved(raw: object) -> list[SavedQuestion]:
    if not isinstance(raw, list):
        return []
    found: list[SavedQuestion] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and isinstance(item.get("answer"), str):
            found.append(SavedQuestion(item["question"], item["answer"]))
    return found


class AnswersRefused(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AnswersInput:
    legal_first_name: str = ""
    legal_last_name: str = ""
    phone: str = ""
    address_line: str = ""
    city: str = ""
    postcode: str = ""
    country: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    right_to_work_uk: str = "unset"
    needs_visa_sponsorship: str = "unset"
    nationality: str = ""
    heard_about: str = ""
    previously_employed: str = "unset"
    free_school_meals: str = PREFER_NOT
    gender: str = PREFER_NOT
    ethnicity: str = PREFER_NOT
    disability: str = PREFER_NOT
    veteran: str = PREFER_NOT
    notice_period: str = ""
    earliest_start_date: str = ""
    willing_to_relocate: str = "unset"
    work_hours: str = ""
    work_location: str = ""
    sexual_orientation: str = PREFER_NOT
    religion: str = PREFER_NOT
    caring_responsibilities: str = PREFER_NOT
    school_type: str = PREFER_NOT
    parental_occupation: str = PREFER_NOT
    # The extra questions (D74) as form text, keyed by question: "yes"/"no", ISO dates, "YYYY-MM" months.
    extra: dict[str, str] = field(default_factory=dict)


ANSWER_FIELDS = tuple(item.name for item in fields(AnswersInput) if item.name != "extra")
_BOOL_FIELDS = frozenset({"right_to_work_uk", "needs_visa_sponsorship", "previously_employed", "willing_to_relocate"})


def _trim(value: str, *, limit: int = MAX_FIELD) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise AnswersRefused(f"Keep each answer under {limit} characters.")
    return cleaned


def _boolish(value: str, *, label: str) -> bool | None:
    cleaned = value.strip().lower()
    if cleaned in ("", "unset"):
        return None
    if cleaned in ("yes", "true", "1"):
        return True
    if cleaned in ("no", "false", "0"):
        return False
    raise AnswersRefused(f"Choose yes or no for {label}.")


def _demo(value: str) -> str:
    cleaned = value.strip() or PREFER_NOT
    if len(cleaned) > MAX_FIELD:
        raise AnswersRefused(f"Keep each answer under {MAX_FIELD} characters.")
    return cleaned


def _url(value: str) -> str | None:
    cleaned = _trim(value, limit=MAX_URL)
    if cleaned is None:
        return None
    lower = cleaned.lower()
    if not lower.startswith(("https://", "http://")):
        raise AnswersRefused("Links must start with http:// or https://.")
    return cleaned


def _choice(value: str, choices: tuple[str, ...], *, label: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned not in choices:
        raise AnswersRefused(f"Choose one of the options for {label}.")
    return cleaned


def _date(value: str, *, label: str, not_before: date | None = None) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise AnswersRefused(f"Enter {label} as a date.") from exc
    if not_before is not None and parsed < not_before:
        raise AnswersRefused(f"Enter {label} as today or a future date.")
    return parsed


def _bool_text(value: bool | None) -> str:
    return "unset" if value is None else "yes" if value else "no"


def _extra(values: dict[str, str]) -> dict[str, str]:
    """The extra answers checked against each question. Blank ones and unknown questions are left out."""
    checked: dict[str, str] = {}
    for key, value in values.items():
        question = EXTRA_BY_KEY.get(key)
        cleaned = (value or "").strip()
        if question is None or not cleaned:
            continue
        if question.kind == "choice":
            if cleaned not in question.choices:
                raise AnswersRefused(f"Choose one of the options for '{question.label}'.")
        elif question.kind == "yes_no":
            cleaned = cleaned.lower()
            if cleaned not in ("yes", "no"):
                raise AnswersRefused(f"Choose yes or no for '{question.label}'.")
        elif question.kind == "date":
            parsed = _date(cleaned, label=f"'{question.label}'")
            if key == "date_of_birth" and parsed is not None and parsed >= date.today():
                raise AnswersRefused("Enter your date of birth as a past date.")
            cleaned = parsed.isoformat() if parsed else ""
        elif question.kind == "month":
            if not _MONTH.fullmatch(cleaned):
                raise AnswersRefused(f"Enter '{question.label}' as a month and year.")
        elif len(cleaned) > MAX_EXTRA_TEXT:
            raise AnswersRefused(f"Keep '{question.label}' under {MAX_EXTRA_TEXT} characters.")
        if cleaned:
            checked[key] = cleaned
    return checked


def _extra_value(key: str, stored: object) -> str | bool | None:
    """One extra answer as the field mapper wants it: yes/no as a bool, diversity ones default to prefer not to say."""
    question = EXTRA_BY_KEY[key]
    text = stored if isinstance(stored, str) and stored else None
    if question.kind == "yes_no":
        return None if text is None else text == "yes"
    if question.prefer_not:
        return text or PREFER_NOT
    return text


def load_answers(session: Session, applicant_id: uuid.UUID) -> ApplicationAnswers | None:
    return session.scalar(select(ApplicationAnswers).where(ApplicationAnswers.applicant_id == applicant_id))


def input_from_row(row: ApplicationAnswers | None) -> AnswersInput:
    """The saved answers as form text, so a page that shows only some of them keeps the rest."""
    if row is None:
        return AnswersInput()
    values: dict[str, str] = {}
    for name in ANSWER_FIELDS:
        value = getattr(row, name)
        if name in _BOOL_FIELDS:
            values[name] = _bool_text(value)
        elif isinstance(value, date):
            values[name] = value.isoformat()
        else:
            values[name] = "" if value is None else str(value)
    extra = {key: str(value) for key, value in (row.extra or {}).items() if key in EXTRA_BY_KEY}
    return AnswersInput(**values, extra=extra)


def _answers_row(session: Session, applicant_id: uuid.UUID) -> ApplicationAnswers:
    row = load_answers(session, applicant_id)
    if row is not None:
        return row
    row = ApplicationAnswers(applicant_id=applicant_id)
    session.add(row)
    return row


def saved_pairs(row: ApplicationAnswers | None) -> list[SavedQuestion]:
    if row is None:
        return []
    return _saved(row.saved_questions)


def save_question_answers(
    session: Session, applicant_id: uuid.UUID, pairs: list[SavedQuestion]
) -> ApplicationAnswers:
    """Remember free-text answers so a later form that asks the same thing can reuse them."""
    if not pairs:
        raise AnswersRefused("Write an answer for each question.")
    if len(pairs) > MAX_SAVED:
        raise AnswersRefused("Too many saved answers.")
    row = _answers_row(session, applicant_id)
    kept: dict[str, SavedQuestion] = {}
    for item in _saved(row.saved_questions):
        kept[question_key(item.question)] = item
    for item in pairs:
        cleaned_question, cleaned_answer = item.question.strip(), item.answer.strip()
        if not cleaned_question or not cleaned_answer:
            raise AnswersRefused("Write an answer for each question.")
        if len(cleaned_question) > 500 or len(cleaned_answer) > MAX_ANSWER:
            raise AnswersRefused(f"Keep each question under 500 characters and each answer under {MAX_ANSWER}.")
        kept[question_key(cleaned_question)] = SavedQuestion(cleaned_question, cleaned_answer)
    if len(kept) > MAX_SAVED:
        raise AnswersRefused("Too many saved answers.")
    row.saved_questions = [{"question": item.question, "answer": item.answer} for item in kept.values()]
    session.flush()
    return row


def save_answers(session: Session, applicant_id: uuid.UUID, raw: AnswersInput) -> ApplicationAnswers:
    """Checks every answer before changing anything, so a refused form leaves the saved row alone."""
    checked = {
        "legal_first_name": _trim(raw.legal_first_name),
        "legal_last_name": _trim(raw.legal_last_name),
        "phone": _trim(raw.phone),
        "address_line": _trim(raw.address_line),
        "city": _trim(raw.city),
        "postcode": _trim(raw.postcode),
        "country": _trim(raw.country) or "United Kingdom",
        "linkedin_url": _url(raw.linkedin_url),
        "github_url": _url(raw.github_url),
        "portfolio_url": _url(raw.portfolio_url),
        "right_to_work_uk": _boolish(raw.right_to_work_uk, label="right to work in the UK"),
        "needs_visa_sponsorship": _boolish(raw.needs_visa_sponsorship, label="visa sponsorship"),
        "nationality": _trim(raw.nationality),
        "heard_about": _trim(raw.heard_about),
        "previously_employed": _boolish(raw.previously_employed, label="previously employed"),
        "notice_period": _choice(raw.notice_period, NOTICE_PERIODS, label="notice period"),
        "earliest_start_date": _date(
            raw.earliest_start_date, label="the earliest start date", not_before=date.today()
        ),
        "willing_to_relocate": _boolish(raw.willing_to_relocate, label="willing to relocate"),
        "work_hours": _choice(raw.work_hours, WORK_HOURS, label="full-time or part-time"),
        "work_location": _choice(raw.work_location, WORK_LOCATIONS, label="where you want to work"),
        **{key: _demo(getattr(raw, key)) for key in _DEMO_KEYS},
        "extra": _extra(raw.extra),
    }
    row = _answers_row(session, applicant_id)
    for name, value in checked.items():
        setattr(row, name, value)
    session.flush()
    return row


def answers_as_map(row: ApplicationAnswers | None, *, email: str) -> dict[str, str | bool | None]:
    """Flat keys the field mapper looks up. Missing row still exposes email."""
    base: dict[str, str | bool | None] = {
        "email": email,
        "legal_first_name": None,
        "legal_last_name": None,
        "full_name": None,
        "phone": None,
        "address_line": None,
        "city": None,
        "postcode": None,
        "country": None,
        "linkedin_url": None,
        "github_url": None,
        "portfolio_url": None,
        "right_to_work_uk": None,
        "needs_visa_sponsorship": None,
        "nationality": None,
        "heard_about": None,
        "previously_employed": None,
        "notice_period": None,
        "earliest_start_date": None,
        "willing_to_relocate": None,
        "work_hours": None,
        "work_location": None,
        "cv": "upload_cv",
        **{key: PREFER_NOT for key in _DEMO_KEYS},
        **{question.key: _extra_value(question.key, None) for question in EXTRA_QUESTIONS},
    }
    if row is None:
        return base
    first, last = row.legal_first_name, row.legal_last_name
    full = " ".join(part for part in (first, last) if part) or None
    base.update(
        {
            "legal_first_name": first,
            "legal_last_name": last,
            "full_name": full,
            "phone": row.phone,
            "address_line": row.address_line,
            "city": row.city,
            "postcode": row.postcode,
            "country": row.country,
            "linkedin_url": row.linkedin_url,
            "github_url": row.github_url,
            "portfolio_url": row.portfolio_url,
            "right_to_work_uk": row.right_to_work_uk,
            "needs_visa_sponsorship": row.needs_visa_sponsorship,
            "nationality": row.nationality,
            "heard_about": row.heard_about,
            "previously_employed": row.previously_employed,
            "notice_period": row.notice_period,
            "earliest_start_date": row.earliest_start_date.isoformat() if row.earliest_start_date else None,
            "willing_to_relocate": row.willing_to_relocate,
            "work_hours": row.work_hours,
            "work_location": row.work_location,
            **{key: getattr(row, key) for key in _DEMO_KEYS},
            **{question.key: _extra_value(question.key, (row.extra or {}).get(question.key)) for question in EXTRA_QUESTIONS},
        }
    )
    # The choice made once in the extra questions wins over the older free-text answer.
    base["heard_about"] = base["heard_about_source"] or row.heard_about
    return base


def document_file_name(session: Session, applicant_id: uuid.UUID, document: str, extension: str) -> str:
    """A file name an employer sees, such as Sam-Taylor-CV.pdf. Without a saved name, just CV.pdf."""
    row = load_answers(session, applicant_id)
    parts = [row.legal_first_name or "", row.legal_last_name or ""] if row is not None else []
    # The header must be plain ASCII, so José becomes Jose rather than Jos.
    plain = [unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode() for part in parts]
    words = [word for part in plain for word in re.findall(r"[A-Za-z0-9]+", part)]
    return "-".join([*words, document]) + f".{extension}"
