"""Degree subjects an advert requires, and whether an applicant's degrees cover them (D45).

Rules only. Each mention of a degree with subjects next to it is one requirement that any of
those subjects meets. Wording such as "or equivalent experience" or "preferably" makes it
optional, and an optional degree, or a subject the rules don't know, never hides a job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


class Field:
    computing = "computing"
    maths = "maths"
    science = "science"
    engineering = "engineering"
    economics = "economics"
    business = "business"
    law = "law"
    design = "design"
    psychology = "psychology"


STEM = frozenset({Field.computing, Field.maths, Field.science, Field.engineering})
QUANTITATIVE = STEM | {Field.economics}

_SUBJECTS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        r"computer science|computing|software engineering|informatics|computer engineering|information technology|"
        r"information systems|data science|artificial intelligence|machine learning|cyber ?security|technology",
        frozenset({Field.computing}),
    ),
    (r"mathematics|maths?|statistics|actuarial", frozenset({Field.maths})),
    (r"physics|chemistry|biology|natural sciences?|life sciences?|bioinformatics|neuroscience",
     frozenset({Field.science})),
    (r"engineering|electrical|electronic|mechanical|aerospace", frozenset({Field.engineering})),
    (r"econometrics|economics", frozenset({Field.economics})),
    (r"business|management|accounting|finance|marketing|commerce", frozenset({Field.business})),
    (r"law|legal", frozenset({Field.law})),
    (r"design", frozenset({Field.design})),
    (r"psychology|social sciences?", frozenset({Field.psychology})),
    (r"stem", STEM),
    (r"quantitative|numerate", QUANTITATIVE),
    (r"technical (?:subject|field|discipline|degree)|scientific (?:subject|field|discipline|degree)", STEM),
)
_SUBJECT = [
    *((re.compile(rf"\b(?i:{words})\b"), fields) for words, fields in _SUBJECTS),
    (re.compile(r"\b(?:CS|IT|AI|ML)\b"), frozenset({Field.computing})),
]

# Acronyms keep their capitals, so "MS" in "MS Office" or "BA" in "BA team" needs a subject after it to count.
_DEGREE = re.compile(
    r"\b(?:(?i:degrees?|bachelor\w*|master'?s|masters|undergraduate|postgraduate)|BSc|MSc|BEng|MEng|BA|BS|MS|PhD|MBA)\b"
    r"(?!\s+(?:Office|Excel|Word|Teams|Project|SQL|Access|Dynamics|Azure|Visio|PowerPoint|Outlook)\b)"
)
_SENTENCE_BREAK = re.compile(r"[!?;]\s|(?<!\be\.g)(?<!\bi\.e)\.\s")
_NOT_A_QUALIFICATION = re.compile(r"\s+of\b", re.IGNORECASE)
_OPTIONAL = re.compile(
    r"\b(?:prefer\w*|desirable|nice to have|bonus|a plus|advantage\w*|ideally|beneficial|"
    r"or equivalent|equivalent (?:\w+ )?experience|in lieu|instead of a degree|"
    r"experience (?:can|may|will|could) (?:replace|substitute)|or (?:a )?portfolio|bootcamp|self[- ]taught|"
    r"compelling narrative|alternative (?:chosen )?path|not (?:required|essential|necessary)|"
    r"no degree|don'?t need|do not need|doesn'?t need|regardless of|do not ask|don'?t ask|no formal|"
    r"(?:level \d|qualification|apprenticeship|diploma|training|experience)\s+or)\b",
    re.IGNORECASE,
)
# Experience offered after the degree can stand in for it, so the job stays (D45). Before the degree,
# "New grad or 0-2 years of experience, with a CS degree" is about seniority, not a way round the degree.
# Lower-case words only, because a capital starts the next bullet: "or a related field Introductory experience".
_EXPERIENCE_INSTEAD = re.compile(
    r"\bor (?:(?!field\b|discipline\b|subject\b|area\b)[a-z0-9][\w-]* ){0,4}experience\b"
)
# Cleaned adverts lose their bullet breaks, so the subjects end where the next requirement begins.
_STOP = re.compile(
    r"\b(?:experience\w*|skills?|knowledge|ability|proven|drive|track record|fluen\w*|minimum|certifications?|"
    r"you|we|what|with|plus|including|from|where|who|which|that|is|are|will|must|strong|excellent|and \d|"
    r"a good level)\b|[!?;]|(?<!\be\.g)(?<!\bi\.e)\.(?=\s)",
    re.IGNORECASE,
)
_WORDS_BEFORE = re.compile(r"(?:[\w'-]+\W+){0,3}$")
_OPTIONAL_BEFORE = 80
_AFTER = 90
_OPTIONAL_AFTER = 130


def fields_of(text: str) -> frozenset[str]:
    """The fields a piece of text names, such as an applicant's "BSc Business Management"."""
    found: set[str] = set()
    for pattern, fields in _SUBJECT:
        if pattern.search(text):
            found |= fields
    return frozenset(found)


def required_fields(description: str | None) -> list[frozenset[str]]:
    """One entry per required degree in the advert: the fields that meet it."""
    text = description or ""
    required = []
    read_to = 0
    for match in _DEGREE.finditer(text):
        # "Bachelor's or Master's degree in ..." is one requirement, already read from its first word.
        if match.start() < read_to:
            continue
        if match.group().lower().startswith("degree") and _NOT_A_QUALIFICATION.match(text, match.end()):
            continue
        before = _WORDS_BEFORE.search(text[max(0, match.start() - 60) : match.start()]).group()
        after = text[match.end() : match.end() + _AFTER]
        if stop := _STOP.search(after):
            after = after[: stop.start()]
        read_to = match.end() + len(after)
        lead_in = text[max(0, match.start() - _OPTIONAL_BEFORE) : match.start()]
        lead_in = lead_in[max((brk.end() for brk in _SENTENCE_BREAK.finditer(lead_in)), default=0) :]
        follow_on = text[match.end() : match.end() + _OPTIONAL_AFTER]
        if _OPTIONAL.search(lead_in) or _OPTIONAL.search(follow_on) or _EXPERIENCE_INSTEAD.search(follow_on):
            continue
        if fields := fields_of(f"{before}{match.group()}{after}"):
            required.append(fields)
    return required


def degree_shuts_out(required: Iterable[frozenset[str]], degrees: list[str]) -> bool:
    """True when the advert requires a subject that none of the applicant's degrees covers.

    Someone who lists no degree isn't shut out: the profile doesn't say what they studied.
    """
    if not degrees:
        return False
    held = frozenset().union(*(fields_of(degree) for degree in degrees))
    return any(not (fields & held) for fields in required)
