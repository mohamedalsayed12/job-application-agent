"""ATS text-layer checks and keyword coverage for a rendered CV PDF."""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from pypdf import PdfReader

Coverage = Literal["covered", "synonym-only", "missing (have it)", "missing (gap)"]

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_FANCY_DASH = re.compile(r"[\u2013\u2014]")


def pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def coverage_for(
    keywords: list[dict[str, str]],
    *,
    extracted: str,
    master_cv: str,
) -> list[dict[str, str]]:
    rows = []
    for item in keywords:
        term = item.get("term", "").strip()
        if not term:
            continue
        status = _status(term, extracted=extracted, master_cv=master_cv)
        rows.append(
            {
                "term": term,
                "priority": item.get("priority", "nice"),
                "rubric_bucket": item.get("rubric_bucket", ""),
                "status": status,
            }
        )
    return rows


def ats_report(
    *,
    pdf_bytes: bytes,
    draft: dict[str, Any],
    keywords: list[dict[str, str]],
    master_cv: str,
) -> dict[str, Any]:
    extracted = pdf_text(pdf_bytes)
    contact = draft.get("contact") if isinstance(draft.get("contact"), dict) else {}
    email = str(contact.get("email", "")).strip()
    phone = str(contact.get("phone", "")).strip()
    checks = {
        "text_extracted": bool(extracted.strip()),
        "email_as_text": (not email) or (email.casefold() in extracted.casefold()) or bool(_EMAIL.search(extracted)),
        "phone_as_text": (not phone) or (phone in extracted) or bool(_PHONE.search(extracted)),
        "ascii_dates": _FANCY_DASH.search(extracted) is None,
        "no_cid_garbage": "(cid:" not in extracted and "\ufffd" not in extracted,
    }
    coverage = coverage_for(keywords, extracted=extracted, master_cv=master_cv)
    return {
        "extractor": "pypdf",
        "checks": checks,
        "coverage": coverage,
        "missing_have_it": [row for row in coverage if row["status"] == "missing (have it)"],
        "passed": all(checks.values()),
    }


def _status(term: str, *, extracted: str, master_cv: str) -> Coverage:
    if _term_in(term, extracted):
        return "covered"
    if _synonym_in(term, extracted):
        return "synonym-only"
    if _term_in(term, master_cv) or _synonym_in(term, master_cv):
        return "missing (have it)"
    return "missing (gap)"


def _term_in(term: str, text: str) -> bool:
    return term.casefold() in text.casefold()


_SYNONYMS = {
    "ci/cd": ("continuous integration", "continuous delivery", "github actions", "gitlab ci"),
    "version control": ("git", "github", "gitlab"),
    "teamwork": ("collaborat", "team player", "pair program"),
    "communication": ("present", "wrote", "document"),
    "problem-solving": ("debug", "troubleshoot", "solved"),
    "testing": ("pytest", "unit test", "test-driven"),
    "cloud": ("aws", "azure", "gcp", "kubernetes", "docker"),
}


def _synonym_in(term: str, text: str) -> bool:
    lower = text.casefold()
    key = term.casefold()
    for needle, alts in _SYNONYMS.items():
        if needle in key or key in needle:
            if any(alt in lower for alt in alts):
                return True
    return False
