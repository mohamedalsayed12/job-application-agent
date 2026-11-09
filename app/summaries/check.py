"""Reject a summary that states a number, date or name the posting doesn't (T35).

Rules only. Anything the check can't tie to the posting counts against the summary, because a
rejected summary costs nothing to the reader: the job shows its description instead.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&+'.-]*")
_SENTENCE_START = re.compile(r"(?:^|[.!?:]\s+|\n\s*)$")
_POSSESSIVE = re.compile(r"(?:'s|')$")

NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred thousand million billion dozen half".split()
)
DATE_WORDS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)
# Ordinary words that open a sentence with a capital without naming anything.
SENTENCE_STARTERS = frozenset(
    "a an the this that these those it its you your we our they their he she i in on at as for with "
    "join work build help lead own design develop support manage apply expect candidates applicants "
    "responsibilities requirements the role team based".split()
)


def _number(text: str) -> str:
    return text.replace(",", "").rstrip(".")


def _word(text: str) -> str:
    return _POSSESSIVE.sub("", text.rstrip(".-")).casefold()


def unsupported(summary: str, posting: str) -> list[str]:
    """Every number, date or name in the summary that the posting doesn't contain, in order."""
    numbers = {_number(found) for found in _NUMBER.findall(posting)}
    words = {_word(found) for found in _WORD.findall(posting)}
    problems: list[str] = []

    for found in _NUMBER.finditer(summary):
        if _number(found.group()) not in numbers:
            problems.append(found.group().rstrip(".,"))

    for found in _WORD.finditer(summary):
        raw = found.group().rstrip(".-")
        word = _word(raw)
        if word in words:
            continue
        if word in NUMBER_WORDS or word in DATE_WORDS and raw[0].isupper():
            problems.append(raw)
        elif raw[0].isupper() and not (_opens_sentence(summary, found.start()) and word in SENTENCE_STARTERS):
            problems.append(raw)
    return list(dict.fromkeys(problems))


def _opens_sentence(text: str, position: int) -> bool:
    return bool(_SENTENCE_START.search(text[:position]))
