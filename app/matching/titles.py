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
NOT_ENTRY_TITLE = re.compile(
    r"\b(senior|snr|sr\.?|staff|principal|lead|manager|head|director|architect|vp|"
    r"intermediate|mid|experienced|ii|iii|iv|"
    r"intern|interns|internship|placement|apprentice|apprenticeship)\b",
    re.IGNORECASE,
)
ENTRY_TITLE = re.compile(r"\b(graduate|grad|junior|jr\.?|entry.level|early.careers?)\b", re.IGNORECASE)
YEARS_REQUIRED = re.compile(
    r"\b([3-9]|1\d)\s*\+?\s*(?:(?:-|–|to)\s*\d+\s*)?(?:years?|yrs?)\b[^.]{0,60}\bexperience",
    re.IGNORECASE,
)


def clean_html(raw: str | None) -> str:
    """Some sources double-encode HTML, so unescape twice before removing tags."""
    text = html.unescape(html.unescape(raw or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
