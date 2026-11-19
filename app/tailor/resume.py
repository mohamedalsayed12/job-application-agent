"""The structured CV every tailoring step reads and writes.

Ported from Resume-Matcher's ResumeData (srbhr/Resume-Matcher@959e458,
apps/backend/app/schemas/models.py) without custom sections, row styles or section metadata,
which only its editor renders.
"""

from __future__ import annotations

import re
from typing import Any

SECTIONS = ("workExperience", "education", "personalProjects")
LIST_FIELDS = ("technicalSkills", "languages", "certificationsTraining", "awards")
PERSONAL_FIELDS = ("name", "title", "email", "phone", "location", "website", "linkedin", "github")

_TEXT = {"type": "string"}
_TEXTS = {"type": "array", "items": _TEXT}


def _object(**properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


RESUME_SCHEMA = _object(
    personalInfo=_object(**{field: _TEXT for field in PERSONAL_FIELDS}),
    summary=_TEXT,
    workExperience={
        "type": "array",
        "items": _object(
            id={"type": "integer"}, title=_TEXT, company=_TEXT, location=_TEXT, years=_TEXT, description=_TEXTS
        ),
    },
    education={
        "type": "array",
        "items": _object(id={"type": "integer"}, institution=_TEXT, degree=_TEXT, years=_TEXT, description=_TEXT),
    },
    personalProjects={
        "type": "array",
        "items": _object(id={"type": "integer"}, name=_TEXT, role=_TEXT, years=_TEXT, description=_TEXTS),
    },
    additional=_object(**{field: _TEXTS for field in LIST_FIELDS}),
)


def all_text(data: dict[str, Any]) -> str:
    """Every word a reader of the CV sees, for keyword matching."""
    parts: list[str] = [str(data.get("summary") or "")]
    for entry in data.get("workExperience") or []:
        parts += [str(entry.get("title", "")), str(entry.get("company", "")), *map(str, entry.get("description") or [])]
    for entry in data.get("education") or []:
        parts += [str(entry.get("degree", "")), str(entry.get("institution", "")), str(entry.get("description") or "")]
    for entry in data.get("personalProjects") or []:
        parts += [str(entry.get("name", "")), str(entry.get("role", "")), *map(str, entry.get("description") or [])]
    additional = data.get("additional") or {}
    for field in LIST_FIELDS:
        parts += map(str, additional.get(field) or [])
    return " ".join(part for part in parts if part)


def keyword_in_text(keyword: str, text: str) -> bool:
    """Whole-term match, so "java" does not match "javascript"."""
    needle = keyword.strip().lower()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text.lower()) is not None


_FILLER = frozenset(
    ["knowledge", "of", "experience", "experienced", "with", "in", "and", "or", "good", "strong", "excellent", "solid", "proven", "working", "understanding", "proficiency", "proficient", "ability", "to", "skills", "skill", "mindset", "databases", "database", "a", "an", "the", "basic", "years"]
)
_SUFFIXES = ("ing", "ions", "ion", "ed", "es", "s")


def term_in_text(term: str, text: str) -> bool:
    """A posting term is in the text: whole, or every skill word in it once filler words are set aside.

    "Knowledge of SQL databases" is met by a CV that says "SQL"; "Knowledge of JavaScript/TypeScript"
    is not met by one that says only "JavaScript".
    """
    if keyword_in_text(term, text):
        return True
    words = [word for word in re.split(r"[\s/,&]+", term.lower()) if word and word not in _FILLER]
    return bool(words) and all(keyword_in_text(word, text) for word in words)


def stem_in_text(term: str, text: str) -> bool:
    """Looser than ``keyword_in_text``: "monitoring" is also found as "monitor" or "monitors"."""
    needle = term.strip().lower()
    if " " not in needle:
        for suffix in _SUFFIXES:
            if needle.endswith(suffix) and len(needle) - len(suffix) >= 4:
                return re.search(rf"(?<!\w){re.escape(needle[: -len(suffix)])}", text.lower()) is not None
    return keyword_in_text(term, text)


_FANCY_DASH = re.compile(r"\s*[–—]\s*")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{2,}")

# section -> (field that names the entry, other fields that must appear in the CV)
_ENTRY_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "workExperience": ("company", ("title", "location")),
    "education": ("institution", ("degree",)),
    "personalProjects": ("name", ("role",)),
}


def ground_parsed(parsed: dict[str, Any], cv: str) -> dict[str, Any] | None:
    """Keep only what the CV text itself shows. None when no section survives.

    Resume-Matcher shows the parsed CV to its user before tailoring. Here nobody sees it, so every
    name, contact detail, skill, year and number the parse returns must appear in the CV text.
    """
    personal = parsed.get("personalInfo") or {}
    summary = str(parsed.get("summary") or "").strip()
    grounded: dict[str, Any] = {
        "personalInfo": {field: _shown(str(personal.get(field, "")), cv) for field in PERSONAL_FIELDS},
        "summary": summary if _from_cv(summary, cv) else "",
    }
    for section, (name_field, fields) in _ENTRY_FIELDS.items():
        entries = []
        for entry in parsed.get(section) or []:
            if not _shown(str(entry.get(name_field, "")), cv):
                continue
            years = _FANCY_DASH.sub(" - ", str(entry.get("years", "")).strip())
            kept = {
                **entry,
                **{field: _shown(str(entry.get(field, "")), cv) for field in fields},
                "id": len(entries) + 1,
                "years": years if all(year in cv for year in _YEAR.findall(years)) else "",
            }
            description = entry.get("description")
            if isinstance(description, list):
                kept["description"] = [row.strip() for row in description if _from_cv(row, cv) and row.strip()]
            else:
                kept["description"] = str(description or "").strip() if _from_cv(str(description or ""), cv) else ""
            entries.append(kept)
        grounded[section] = entries
    additional = parsed.get("additional") or {}
    grounded["additional"] = {
        field: [item.strip() for item in additional.get(field) or [] if _shown(item, cv)] for field in LIST_FIELDS
    }
    if not any(grounded[section] for section in SECTIONS) and not grounded["additional"]["technicalSkills"]:
        return None
    return grounded


def _shown(value: str, cv: str) -> str:
    value = value.strip()
    return value if keyword_in_text(value, cv) else ""


def _from_cv(text: str, cv: str) -> bool:
    """Every number, and two in three words, of a parsed sentence appear in the CV."""
    if any(number not in cv for number in _NUMBER.findall(text)):
        return False
    words = _WORD.findall(text)
    return sum(keyword_in_text(word, cv) for word in words) * 3 >= len(words) * 2
