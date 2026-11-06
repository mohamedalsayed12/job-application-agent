"""Company-name cleaning for candidate rows (MVP §9)."""

from __future__ import annotations

import re

_SUFFIXES = re.compile(
    r"\b(limited|ltd|plc|llc|inc|corp|corporation|co)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_SPACES = re.compile(r"\s+")


def clean_company_name(name: str) -> str:
    """Lowercase, drop Ltd/Limited/PLC-style suffixes and punctuation."""
    text = name.strip().lower()
    text = _SUFFIXES.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _SPACES.sub(" ", text).strip()
