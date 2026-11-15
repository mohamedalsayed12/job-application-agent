"""Reject outreach drafts that invent quoted phrases not in the research evidence."""

from __future__ import annotations

import re

_QUOTED = re.compile(r"[\"“”]([^\"“”]{12,200})[\"“”]")


def outreach_unsupported(
    text: str,
    *,
    sources: str,
    allowed_quotes: list[str],
    contact_name: str | None = None,
) -> list[str]:
    """Only quote invention is checked.

    The job-summary fact check rejects first-person wording that is normal in outreach,
    so it is not reused here. ``sources`` and ``contact_name`` are kept for call-site
    compatibility and future tightening.
    """
    _ = sources, contact_name
    allowed = [re.sub(r"\s+", " ", q.strip()) for q in allowed_quotes if q and q.strip()]
    problems: list[str] = []
    for match in _QUOTED.finditer(text):
        phrase = re.sub(r"\s+", " ", match.group(1).strip())
        if len(phrase) < 12:
            continue
        if not any(_phrase_from_quote(phrase, quote) for quote in allowed):
            problems.append(f'invented quote: "{phrase[:60]}"')
    return problems


def _phrase_from_quote(phrase: str, quote: str) -> bool:
    """The quoted phrase must appear inside an allowed research quote (not the reverse)."""
    needle = phrase.lower()
    hay = quote.lower()
    return needle in hay
