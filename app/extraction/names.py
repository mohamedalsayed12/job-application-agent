"""Company-name cleaning for candidate rows (MVP §9)."""

from __future__ import annotations

import re

# Trailing legal suffixes only (after punctuation is flattened to spaces).
_TRAILING_SUFFIX = re.compile(
    r"\b(limited|ltd|plc|llc|inc|corp|corporation)\s*$",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_SPACES = re.compile(r"\s+")


def clean_company_name(name: str) -> str:
    """Lowercase, drop trailing Ltd/Limited/PLC-style suffixes and punctuation."""
    text = _NON_ALNUM.sub(" ", name.strip().lower())
    text = _SPACES.sub(" ", text).strip()
    while True:
        cleaned = _TRAILING_SUFFIX.sub("", text).strip()
        if cleaned == text:
            return cleaned
        text = cleaned
