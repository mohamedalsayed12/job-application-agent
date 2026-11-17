"""Decide whether a public post is someone who passed this role.

A success count needs all three: the writer lived it, they passed or got an offer,
and the role is the one this pack is for. Recruiters, coaches and course sellers
are named as what they are. A neighbouring job is adjacent, not the same role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPACE = re.compile(r"\s+")
_LEVEL = re.compile(
    r"\b(graduate|junior|senior|entry[\s-]?level|new grad|intern|internship)\b",
    re.IGNORECASE,
)
_SECOND_HAND: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "course_seller",
        re.compile(
            r"\b(my course|our course|enroll now|use (my )?code|link in bio|"
            r"i (sell|created|launched) (a |my )?course)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "coach",
        re.compile(
            r"\b(career coach|interview coach|i coach|my coaching|book a (call|session|1:1)|"
            r"discovery call|i help (candidates|people|engineers|developers)( get hired| (land|get|pass)))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "recruiter",
        re.compile(
            r"\b(i'?m a recruiter|i am a recruiter|as a recruiter|talent acquisition|"
            r"we'?re hiring|i'?m hiring|hiring for a |ex[- ][a-z]+ recruiter)\b",
            re.IGNORECASE,
        ),
    ),
)
_LIVED = re.compile(
    r"\b(i (got|received|accepted) (an |the )?offer|got the offer|got an offer|"
    r"they offered me|i was (rejected|offered)|i (passed|failed)|my interview|"
    r"i interviewed|i got rejected)\b",
    re.IGNORECASE,
)
_OFFER = re.compile(
    r"\b(got the offer|got an offer|received (an |the )?offer|they offered me|"
    r"i was offered|accepted the offer)\b",
    re.IGNORECASE,
)
_PASSED_INTERVIEW = re.compile(r"\bi passed\b", re.IGNORECASE)
_INTERVIEW = re.compile(r"\binterviews?\b", re.IGNORECASE)

# Neighbouring titles that must not count as each other. Aliases are the same role.
_FAMILIES: dict[str, tuple[str, ...]] = {
    "software engineer": ("software engineer", "software developer", "software engineering"),
    "data analyst": ("data analyst", "data analytics"),
    "data scientist": ("data scientist", "data science"),
    "data engineer": ("data engineer", "data engineering"),
    "business analyst": ("business analyst",),
    "backend developer": ("backend developer", "back-end developer", "backend engineer", "back end developer"),
    "python developer": ("python developer",),
    "frontend developer": ("frontend developer", "front-end developer", "front end developer"),
}
_VOICE_NOTE = {
    "recruiter": "Recruiter, not a person who passed.",
    "coach": "Coach, not a person who passed.",
    "course_seller": "Course seller, not a person who passed.",
}
ADJACENT_NOTE = "Adjacent role, not this role."


@dataclass(frozen=True, slots=True)
class EvidenceStance:
    voice: str
    role_fit: str
    counts_as_passed: bool
    employer_match: bool | None = None

    def note(self) -> str:
        """Label a reader should see when this post is not someone who passed this role."""
        parts: list[str] = []
        voice_note = _VOICE_NOTE.get(self.voice)
        if voice_note:
            parts.append(voice_note)
        if self.role_fit == "adjacent":
            parts.append(ADJACENT_NOTE)
        if self.employer_match is False:
            parts.append("Different employer.")
        return " ".join(parts)


def canonical_role(role: str) -> str:
    stripped = _LEVEL.sub(" ", role.casefold())
    return _SPACE.sub(" ", stripped).strip()


def read_stance(role: str, title: str, snippet: str, *, company: str | None = None) -> EvidenceStance:
    if role.startswith("company:"):
        pieces = role.split(":", 2)
        if len(pieces) == 3:
            role = pieces[2]
    text = _SPACE.sub(" ", f"{title}\n{snippet}").strip()
    if re.search(r"\bex[- ][a-z]+ recruiter\b", title, re.IGNORECASE):
        voice = "recruiter"
    else:
        voice = _voice(text)
    role_fit = _role_fit(canonical_role(role), text.casefold())
    passed = voice == "first_hand" and role_fit == "same" and _passed(text)
    employer_match = None
    if company is not None and company.strip():
        employer_match = company.casefold().strip() in text.casefold()
        if employer_match is False:
            passed = False
    return EvidenceStance(
        voice=voice,
        role_fit=role_fit,
        counts_as_passed=passed,
        employer_match=employer_match,
    )


def _voice(text: str) -> str:
    for name, pattern in _SECOND_HAND:
        if pattern.search(text):
            return name
    if _LIVED.search(text):
        return "first_hand"
    return "unclear"


def _passed(text: str) -> bool:
    if _OFFER.search(text):
        return True
    return bool(_PASSED_INTERVIEW.search(text) and _INTERVIEW.search(text))


def _role_fit(role: str, text: str) -> str:
    if not role:
        return "other"
    own = _FAMILIES.get(role, (role,))
    if any(_phrase(alias, text) for alias in own):
        return "same"
    for family, aliases in _FAMILIES.items():
        if family == role:
            continue
        if any(_phrase(alias, text) for alias in aliases):
            return "adjacent"
    return "other"


def _phrase(phrase: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None
