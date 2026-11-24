"""Plain-text and title rules shared by cleaning and matching. Pure: no database, no network."""

from __future__ import annotations

import html
import re

UK_PLACE = re.compile(
    r"\b(united kingdom|uk|gb|great britain|england|scotland|wales|northern ireland|"
    r"london|manchester|salford|birmingham|leeds|sheffield|liverpool|bristol|cambridge|"
    r"oxford|edinburgh|glasgow|cardiff|belfast|newcastle|nottingham|reading|hatfield)\b",
    re.IGNORECASE,
)
_TRAINING = (
    r"intern|interns|internship|internships|placement|placements|"
    r"apprentice|apprentices|apprenticeship|apprenticeships"
)
TRAINING_TITLE = re.compile(rf"\b({_TRAINING})\b", re.IGNORECASE)
INTERNSHIP_TITLE = re.compile(
    r"\b(intern|interns|internship|internships|placement|placements)\b",
    re.IGNORECASE,
)
APPRENTICE_TITLE = re.compile(
    r"\b(apprentice|apprentices|apprenticeship|apprenticeships)\b",
    re.IGNORECASE,
)
NOT_ENTRY_TITLE = re.compile(
    rf"\b(senior|snr|sr\.?|staff|principal|lead|manager|head|director|architect|vp|"
    rf"intermediate|mid|experienced|ii|iii|iv|{_TRAINING})\b",
    re.IGNORECASE,
)
# Shared entry-level wording. Bare "entry" is strip-only: matching must not treat
# "Data Entry Analyst" / "Market Entry Manager" as entry-level jobs or roles.
ENTRY_TITLE_TOKENS = (
    "graduate",
    "grad",
    "junior",
    r"jr\.?",
    r"entry[- ]level",
    r"early[- ]careers?",
)
LEADING_ENTRY_LEVEL_TOKENS = (*ENTRY_TITLE_TOKENS, "entry")
ENTRY_TITLE = re.compile(rf"\b(?:{'|'.join(ENTRY_TITLE_TOKENS)})\b", re.IGNORECASE)
LEADING_ENTRY_LEVEL = re.compile(rf"^(?:{'|'.join(LEADING_ENTRY_LEVEL_TOKENS)})\s+", re.IGNORECASE)
YEARS_REQUIRED = re.compile(
    r"\b([3-9]|1\d)\s*\+?\s*(?:(?:-|–|to)\s*\d+\s*)?(?:years?|yrs?)\b[^.]{0,60}\bexperience",
    re.IGNORECASE,
)


def clean_html(raw: str | None) -> str:
    """Some sources double-encode HTML, so unescape twice before removing tags."""
    text = html.unescape(html.unescape(raw or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
