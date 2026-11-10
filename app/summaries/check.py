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
    "responsibilities requirements the role team based "
    "key main core required desired preferred essential ideal typical successful skills experience duties "
    "benefits day".split()
)
# A number in words matches the same number in digits: "three days" for "3 days in the office".
_DIGITS = {
    word: str(value)
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen twenty".split()
    )
}
# Second halves of a compound that add no fact: "London-based", "customer-facing".
_COMPOUND_ENDS = frozenset(
    "based level facing focused driven oriented related time side end scale wide first heavy friendly".split()
)


# Names that mean the same thing (operator decision, D47). When the posting has any form, the
# summary may use any other. Abbreviations in capitals match only in capitals: "US", not "join us".
EQUIVALENTS: tuple[tuple[str, ...], ...] = (
    ("UK", "United Kingdom", "Great Britain", "Britain"),
    ("US", "USA", "United States"),
    ("EMEA", "Europe, the Middle East and Africa", "Europe, Middle East and Africa"),
    ("APAC", "Asia Pacific", "Asia-Pacific"),
    ("CI", "continuous integration"),
    ("CD", "continuous delivery", "continuous deployment"),
    ("FX", "foreign exchange"),
    ("AI", "artificial intelligence"),
    ("ML", "machine learning"),
    ("LLM", "large language model"),
    ("NLP", "natural language processing"),
    ("API", "application programming interface"),
    ("SaaS", "software as a service"),
    ("B2B", "business to business", "business-to-business"),
    ("QA", "quality assurance"),
    ("SRE", "site reliability engineering", "site reliability engineer"),
    ("UI", "user interface"),
    ("UX", "user experience"),
    ("HR", "human resources"),
    ("K8s", "Kubernetes"),
    ("GCP", "Google Cloud"),
    ("AWS", "Amazon Web Services"),
    ("JS", "JavaScript"),
    ("TS", "TypeScript"),
    ("Postgres", "PostgreSQL"),
    ("Mongo", "MongoDB"),
)


def _form_pattern(form: str) -> re.Pattern[str]:
    body = r"[\s,-]+".join(re.escape(part) for part in re.split(r"[\s,-]+", form))
    return re.compile(rf"(?<!\w){body}(?!\w)", 0 if form.isupper() else re.IGNORECASE)


_EQUIVALENT_FORMS = [[(_form_pattern(form), form) for form in group] for group in EQUIVALENTS]


def _equivalent_words(posting: str) -> set[str]:
    """Words the summary may use because the posting names the same thing another way."""
    allowed: set[str] = set()
    for group in _EQUIVALENT_FORMS:
        if any(pattern.search(posting) for pattern, _ in group):
            allowed |= {_word(found) for _, form in group for found in _WORD.findall(form)}
    return allowed


def _number(text: str) -> str:
    return text.replace(",", "").rstrip(".")


def _word(text: str) -> str:
    return _POSSESSIVE.sub("", text.rstrip(".-")).casefold()


def unsupported(summary: str, posting: str) -> list[str]:
    """Every number, date or name in the summary that the posting doesn't contain, in order."""
    numbers = {_number(found) for found in _NUMBER.findall(posting)}
    words = {_word(found) for found in _WORD.findall(posting)} | _equivalent_words(posting)
    # "English-language" and "Git-based" in the posting support "English" and "Git" in the summary.
    words |= {part for word in words if "-" in word for part in word.split("-") if part}
    problems: list[str] = []

    for found in _NUMBER.finditer(summary):
        if _number(found.group()) not in numbers:
            problems.append(found.group().rstrip(".,"))

    for found in _WORD.finditer(summary):
        raw = found.group().rstrip(".-")
        word = _word(raw)
        if _in_posting(word, words) or _same_number_in_posting(word, summary[found.end() :], posting):
            continue
        if "-" in word and all(_in_posting(part, words) or part in _COMPOUND_ENDS for part in word.split("-")):
            continue
        if word in NUMBER_WORDS or word in DATE_WORDS and raw[0].isupper():
            problems.append(raw)
        elif raw[0].isupper() and not (_opens_sentence(summary, found.start()) and word in SENTENCE_STARTERS):
            problems.append(raw)
    return list(dict.fromkeys(problems))


def _same_number_in_posting(word: str, rest: str, posting: str) -> bool:
    """ "three days" is supported by "3 days", not by a 3 counting something else."""
    digit, following = _DIGITS.get(word), re.match(r"\s+([A-Za-z]+)", rest)
    if digit is None or following is None:
        return False
    return re.search(rf"(?<!\d){digit}\s+{re.escape(following.group(1))}", posting, re.IGNORECASE) is not None


def _in_posting(word: str, words: set[str]) -> bool:
    """The word, or its singular or plural, is in the posting: "API" for "APIs"."""
    return word in words or f"{word}s" in words or (word.endswith("s") and word[:-1] in words)


def _opens_sentence(text: str, position: int) -> bool:
    return bool(_SENTENCE_START.search(text[:position]))
