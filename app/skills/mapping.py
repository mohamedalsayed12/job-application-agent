"""Find skills in text by exact name, so profiles and jobs land on the same taxonomy entries (T33).

Rules only: no fetching and no AI. Names are looked up whole, never by similarity, so
"react to changes" can't count as the skill React.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.models import SkillMatchMethod

_TOKEN = re.compile(r"[a-z0-9+#][a-z0-9+#.]*|\.[a-z0-9]+", re.IGNORECASE)
_TRAILING = "."
_PARENTHESES_AT_END = re.compile(r"\s*\([^)]*\)$")
MAX_LABEL_WORDS = 5

# Ordinary words in lower case, so an advert must use these capitals: "Excel", not "excel at".
CASE_SENSITIVE_LABELS = frozenset(
    "angular assembly bash chef eclipse excel express flask groovy less maven ml mocha pascal puppet "
    "r rails react ruby rust salt sass scala scratch snowflake spark swift tableau".split()
)
# Too common to mean the skill in an advert at all. A person can still choose these on their profile.
NOT_IN_JOB_TEXT = frozenset(
    "access absorb algorithm algorithmic algorithms brainstorm canvas chips computer database formulae formulas "
    "go google ict innovate ort processor qve scripting scrutinize st transformer wp yahoo".split()
) | {
    "computer science",  # A degree subject in adverts, not a skill.
    "iot",  # Almost always in an employer's description of itself. "Internet of Things" still counts.
}


def normalize_label(text: str) -> str:
    """The one form every name is compared in: lower case, single spaces, no trailing full stop."""
    return " ".join(text.casefold().split()).rstrip(_TRAILING)


def label_forms(label: str) -> list[str]:
    """The name itself and, for "Python (computer programming)", the name people actually write."""
    short = _PARENTHESES_AT_END.sub("", label).strip()
    return [label] if short == label or not short else [label, short]


@dataclass(frozen=True, slots=True)
class Hit:
    skill_id: uuid.UUID
    matched_text: str
    how_matched: SkillMatchMethod


@dataclass(frozen=True, slots=True)
class LabelEntry:
    skill_id: uuid.UUID
    label: str
    is_preferred: bool


def _method(entry: LabelEntry) -> SkillMatchMethod:
    return SkillMatchMethod.exact if entry.is_preferred else SkillMatchMethod.alternative_label


def _counts_in_advert(key: str, original: str, entry: LabelEntry, following: str) -> bool:
    if key in NOT_IN_JOB_TEXT:
        return False
    if key in CASE_SENSITIVE_LABELS and original != entry.label:
        return False
    # "Excel at stakeholder management" opens a sentence in real adverts, and isn't the spreadsheet.
    if key == "excel" and following.startswith(" at "):
        return False
    # "R&D" is not the language R.
    return not following.startswith("&")


def find_in_text(text: str, labels: dict[str, LabelEntry]) -> list[Hit]:
    """Every skill named in the text, once each. A longer name beats the shorter one inside it."""
    spans = [(match.start(), match.end()) for match in _TOKEN.finditer(text)]
    found: dict[uuid.UUID, Hit] = {}
    position = 0
    while position < len(spans):
        step = 1
        for size in range(min(MAX_LABEL_WORDS, len(spans) - position), 0, -1):
            start, end = spans[position][0], spans[position + size - 1][1]
            original = text[start:end].rstrip(_TRAILING)
            key = normalize_label(original)
            entry = labels.get(key)
            if entry is None or not _counts_in_advert(key, original, entry, text[end:end + 4]):
                continue
            found.setdefault(entry.skill_id, Hit(entry.skill_id, original, _method(entry)))
            step = size
            break
        position += step
    return list(found.values())
