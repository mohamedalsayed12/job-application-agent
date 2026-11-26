"""Visa sponsorship signals in job adverts (D79).

Hard rule: when the applicant needs sponsorship, adverts that clearly refuse it are excluded
from matches (same idea as salary ``below``). Soft signals such as "licensed sponsor" are
hypothesis-only and never decide a match; they do not scrape the Home Office register.
"""

from __future__ import annotations

import re

# Phrases that mean the employer will not sponsor (UK graduate / Skilled Worker adverts).
# "Right to work" / "authorised to work" need UK/visa framing or a clear end of phrase so
# hybrid wording ("right to work from home", "authorised to work on client sites") is not a refusal.
_REFUSES = re.compile(
    r"(?:"
    r"unable to sponsor|cannot sponsor|can't sponsor|will not sponsor|won't sponsor|"
    r"do not sponsor|does not sponsor|don't sponsor|"
    r"no sponsorship|not able to sponsor|unable to provide sponsorship|"
    r"do not offer sponsorship|does not offer sponsorship|don't offer sponsorship|"
    r"sponsorship is not available|no visa sponsorship|visas? cannot be sponsored|"
    r"must (?:already )?have (?:existing )?(?:uk )?work (?:authori[sz]ation|permit)|"
    r"only (?:consider|accept) (?:candidates|applicants) with (?:existing )?(?:the )?right to work"
    r"(?!\s+from\s+home)(?:\s+in\s+the\s+uk)?(?=\s*[.,;:)\]-]|\s+(?:to\s+(?:live|reside)|(?:and|,)\s)|\s*$)|"
    r"(?:candidates\s+)?must (?:already )?have (?:the )?right to work(?!\s+from\s+home)"
    r"(?:\s+in\s+the\s+uk)?(?=\s*[.,;:)\]-]|\s+(?:to\s+(?:live|reside)|(?:and|,)\s)|\s*$)|"
    r"right to work(?:\s+in\s+the\s+uk)?(?:\s+is|\s+are)\s+essential|"
    r"you must (?:already )?be (?:eligible|authori[sz]ed) to work(?:\s+in\s+the\s+uk|\s+in\s+uk)"
    r")",
    re.IGNORECASE,
)

# Weak positive hint only — never treated as a confirmed offer to sponsor (D79).
# (?<!un) keeps "unable to sponsor" from counting as a positive "able to sponsor" hit.
_MAY_SPONSOR = re.compile(
    r"(?:"
    r"licensed sponsor|licence to sponsor|license to sponsor|sponsor licence|sponsor license|"
    r"home office sponsor(?:ship)? register|on the (?:home office )?sponsor(?:ship)? register|"
    r"visa sponsorship (?:is )?available|(?<!un)able to sponsor|we (?:can|will) sponsor|"
    r"sponsorship (?:may be |is )?available|happy to sponsor"
    r")",
    re.IGNORECASE,
)


def refuses_sponsorship(description: str | None) -> bool:
    """True when the advert clearly says the employer will not sponsor."""
    return bool(description and _REFUSES.search(description))


def may_sponsor_signal(description: str | None) -> bool:
    """Non-binding hypothesis from listing text only. Never a confirmed sponsor status."""
    return bool(description and _MAY_SPONSOR.search(description))


def job_excluded_for_sponsorship(*, needs_visa_sponsorship: bool | None, description: str | None) -> bool:
    """Exclude when the person needs sponsorship and the advert refuses it."""
    return bool(needs_visa_sponsorship) and refuses_sponsorship(description)
