"""Load and save employer-application answers (separate from the job-search profile)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PREFER_NOT, ApplicationAnswers

MAX_FIELD = 200
MAX_URL = 500
MAX_ANSWER = 2000
MAX_SAVED = 40
_DEMO_KEYS = ("free_school_meals", "gender", "ethnicity", "disability", "veteran")
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


def _answers_row(session: Session, applicant_id: uuid.UUID) -> ApplicationAnswers:
    row = load_answers(session, applicant_id)
    if row is not None:
        return row
    row = ApplicationAnswers(
        applicant_id=applicant_id,
        free_school_meals=PREFER_NOT,
        gender=PREFER_NOT,
        ethnicity=PREFER_NOT,
        disability=PREFER_NOT,
        veteran=PREFER_NOT,
    )
    session.add(row)
    return row


class AnswersRefused(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AnswersInput:
    legal_first_name: str
    legal_last_name: str
    phone: str
    address_line: str
    city: str
    postcode: str
    country: str
    linkedin_url: str
    github_url: str
    portfolio_url: str
    right_to_work_uk: str
    needs_visa_sponsorship: str
    nationality: str
    heard_about: str
    previously_employed: str
    free_school_meals: str
    gender: str
    ethnicity: str
    disability: str
    veteran: str


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


def load_answers(session: Session, applicant_id: uuid.UUID) -> ApplicationAnswers | None:
    return session.scalar(select(ApplicationAnswers).where(ApplicationAnswers.applicant_id == applicant_id))


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
    row = _answers_row(session, applicant_id)
    row.legal_first_name = _trim(raw.legal_first_name)
    row.legal_last_name = _trim(raw.legal_last_name)
    row.phone = _trim(raw.phone)
    row.address_line = _trim(raw.address_line)
    row.city = _trim(raw.city)
    row.postcode = _trim(raw.postcode)
    row.country = _trim(raw.country) or "United Kingdom"
    row.linkedin_url = _url(raw.linkedin_url)
    row.github_url = _url(raw.github_url)
    row.portfolio_url = _url(raw.portfolio_url)
    row.right_to_work_uk = _boolish(raw.right_to_work_uk, label="right to work in the UK")
    row.needs_visa_sponsorship = _boolish(raw.needs_visa_sponsorship, label="visa sponsorship")
    row.nationality = _trim(raw.nationality)
    row.heard_about = _trim(raw.heard_about)
    row.previously_employed = _boolish(raw.previously_employed, label="previously employed")
    row.free_school_meals = _demo(raw.free_school_meals)
    row.gender = _demo(raw.gender)
    row.ethnicity = _demo(raw.ethnicity)
    row.disability = _demo(raw.disability)
    row.veteran = _demo(raw.veteran)
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
        "cv": "upload_cv",
        **{key: PREFER_NOT for key in _DEMO_KEYS},
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
            "free_school_meals": row.free_school_meals,
            "gender": row.gender,
            "ethnicity": row.ethnicity,
            "disability": row.disability,
            "veteran": row.veteran,
        }
    )
    return base
