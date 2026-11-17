"""Decide whether a public post is someone who passed this role.

A success count needs all three: the writer lived it, they passed or got an offer,
and the role is the one this pack is for. The pass has to be the writer's own
words in the post body. A friend, a title, or declining the offer does not count.
Recruiters, coaches and course sellers are named as what they are.
A neighbouring job is adjacent, not the same role.
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
            r"\b(my course|our course|buy (the |my |our )?course|enroll( now)?|"
            r"discount code|use my code|promo code|link in bio|book a call|"
            r"i (sell|launched) (a |my )?course|i created (a |my )?course(?!\s+project\b))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "coach",
        re.compile(
            r"\b(career coach|interview coach|i coach|my coaching|book a (session|1:1)|"
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
    r"\b(i (got|received|accepted) (an |the )?offer|they offered me|"
    r"i was (rejected|offered)|i (passed|failed)|my interview|"
    r"i interviewed|i got rejected)\b",
    re.IGNORECASE,
)
# The writer's "I" can have a few words before the offer. Stop at someone else's subject.
_OFFER = re.compile(
    r"\b(?:i\b(?:\s+(?!my\s+friend\b|she\b|he\b|they\b|someone\b|somebody\b)[\w']+\b[,;:]?){0,12}\s+"
    r"(?:got|received|accepted)\s+(?:an |the )?offer|they offered me|i was offered)\b",
    re.IGNORECASE,
)
# "passed on" is a decline, not a pass.
_PASSED_INTERVIEW = re.compile(r"\bi passed\b(?!\s+on\b)", re.IGNORECASE)
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
NOT_FIRST_HAND = "Not a first-hand account."


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

    def outcome_for(self, outcome: str) -> str:
        """A model success counts only when this post is someone who passed this role."""
        if outcome == "success" and not self.counts_as_passed:
            return "unclear"
        return outcome

    def reasons_with_note(self, reasons: list[str]) -> list[str]:
        note = self.note()
        if note and note not in reasons:
            reasons = [note, *reasons]
        return reasons[:8]

    @staticmethod
    def reader_label(cited: list[EvidenceStance], detail: str = "") -> str:
        """Reader-facing label for a theme. Kept beside detail, never inside it."""
        notes: list[str] = []
        for stance in cited:
            note = stance.note()
            if note and note not in notes and note not in detail:
                notes.append(note)
        if cited and all(stance.voice == "unclear" for stance in cited) and NOT_FIRST_HAND not in detail:
            if NOT_FIRST_HAND not in notes:
                notes.append(NOT_FIRST_HAND)
        return " ".join(notes)

    @staticmethod
    def success_earned(cited: list[EvidenceStance | None]) -> bool:
        """A success theme stands only when every cite is a post that counts."""
        return bool(cited) and all(item is not None and item.counts_as_passed for item in cited)


def canonical_role(role: str) -> str:
    stripped = _LEVEL.sub(" ", role.casefold())
    return _SPACE.sub(" ", stripped).strip()


def read_stance(role: str, title: str, snippet: str, *, company: str | None = None) -> EvidenceStance:
    title = _SPACE.sub(" ", title).strip()
    text = _SPACE.sub(" ", f"{title}\n{snippet}").strip()
    body = _writer_text(title, snippet)
    voice = _voice(title, text, body)
    role_fit = _role_fit(canonical_role(role), text.casefold())
    passed = voice == "first_hand" and role_fit == "same" and _passed(body, text)
    employer_match = None
    if company is not None and company.strip():
        employer_match = _phrase(_SPACE.sub(" ", company.casefold()).strip(), text.casefold())
        if employer_match is False:
            passed = False
    return EvidenceStance(
        voice=voice,
        role_fit=role_fit,
        counts_as_passed=passed,
        employer_match=employer_match,
    )


def _writer_text(title: str, snippet: str) -> str:
    """Drop a leading title. Stored snippets repeat it, and a title is not the writer passing."""
    snippet = _SPACE.sub(" ", snippet).strip()
    folded_title = title.casefold()
    folded = snippet.casefold()
    if folded_title and folded.startswith(folded_title):
        return folded[len(folded_title) :].strip()
    return snippet


def _voice(title: str, text: str, body: str) -> str:
    """A title that already matches a second-hand pattern wins, in that pattern's order."""
    for sample in (title, text):
        for name, pattern in _SECOND_HAND:
            if pattern.search(sample):
                return name
    if _LIVED.search(body) or _OFFER.search(body):
        return "first_hand"
    return "unclear"


def _passed(body: str, text: str) -> bool:
    if _OFFER.search(body):
        return True
    return bool(_PASSED_INTERVIEW.search(body) and _INTERVIEW.search(text))


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
