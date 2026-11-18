"""ATS text-layer checks and keyword coverage for a rendered CV PDF."""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from pypdf import PdfReader

from app.tailor.refiner import job_keyword_terms
from app.tailor.resume import all_text, term_in_text

Coverage = Literal["covered", "missing (have it)", "missing (gap)"]

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_FANCY_DASH = re.compile(r"[–—]")
_PRIORITIES = (("required_skills", "required"), ("preferred_skills", "preferred"), ("keywords", "keyword"))


def pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def ats_report(
    *,
    pdf_bytes: bytes,
    resume: dict[str, Any],
    master: dict[str, Any],
    keywords: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """What an ATS reads from the PDF, and which posting terms it finds there."""
    extracted = pdf_text(pdf_bytes)
    personal = resume.get("personalInfo") or {}
    email = str(personal.get("email", "")).strip()
    phone = str(personal.get("phone", "")).strip()
    checks = {
        "text_extracted": bool(extracted.strip()),
        "email_as_text": (not email) or email.casefold() in extracted.casefold() or bool(_EMAIL.search(extracted)),
        "phone_as_text": (not phone) or phone in extracted or bool(_PHONE.search(extracted)),
        "ascii_dates": _FANCY_DASH.search(extracted) is None,
        "no_cid_garbage": "(cid:" not in extracted and "�" not in extracted,
    }
    coverage = coverage_for(keywords, extracted=" ".join(extracted.split()), master_text=all_text(master))
    covered = sum(row["status"] == "covered" for row in coverage)
    return {
        "extractor": "pypdf",
        "checks": checks,
        "keyword_match": round(covered * 100 / len(coverage)) if coverage else 0,
        "coverage": coverage,
        "warnings": warnings,
        "passed": all(checks.values()),
    }


def coverage_for(keywords: dict[str, Any], *, extracted: str, master_text: str) -> list[dict[str, str]]:
    priority: dict[str, str] = {}
    for field, label in _PRIORITIES:
        for term in keywords.get(field) or []:
            priority.setdefault(term, label)
    return [
        {"term": term, "priority": priority[term], "status": _status(term, extracted, master_text)}
        for term in job_keyword_terms(keywords)
    ]


def _status(term: str, extracted: str, master_text: str) -> Coverage:
    if term_in_text(term, extracted):
        return "covered"
    if term_in_text(term, master_text):
        return "missing (have it)"
    return "missing (gap)"
