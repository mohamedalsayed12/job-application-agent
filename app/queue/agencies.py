"""Recruitment agencies are not target employers (Step 10 decision D-A).

Agencies post jobs for other companies and never run their own Greenhouse or
Lever board, so they are dropped before they become candidates. This keeps
them out of CF3 searches and the verification queue. Their job postings are
still collected later as agency jobs (MVP job format: employer type).
"""

from __future__ import annotations

import re
from typing import Final

from app.queue.names import clean_company_name

# Whole words in the cleaned name that mark an agency.
_AGENCY_WORDS: Final = re.compile(
    r"\b(recruit|recruitment|recruiting|recruiters?|resourcing|staffing|headhunters?|"
    r"employment agency|executive search|search and selection|"
    r"talent solutions)\b"
)

# Agencies whose names carry none of the words above: the ones confirmed in the
# Step 10 audit (homepage title says "recruitment agency") plus large UK agencies.
KNOWN_AGENCIES: Final[frozenset[str]] = frozenset(
    {
        "adecco",
        "adria solutions",
        "braundton",
        "computer futures",
        "experis",
        "forward role",
        "harvey nash",
        "hays",
        "lorien",
        "manpower",
        "matchtech",
        "michael page",
        "michael page technology",
        "oliver james",
        "oliver james associates",
        "page group",
        "picture more",
        "randstad",
        "randstad technologies",
        "robert half",
        "robert walters",
        "sthree",
        "tenth revolution group",
        "viqu",
    }
)


def is_recruitment_agency(company_name: str) -> bool:
    cleaned = clean_company_name(company_name)
    if not cleaned:
        return False
    return cleaned in KNOWN_AGENCIES or _AGENCY_WORDS.search(cleaned) is not None
