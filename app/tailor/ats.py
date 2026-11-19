"""ATS text-layer checks and keyword coverage for a rendered CV PDF and DOCX."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Literal

from docx import Document
from pypdf import PdfReader

from app.tailor.refiner import job_keyword_terms
from app.tailor.render import blocks
from app.tailor.resume import all_text, term_in_text

Coverage = Literal["covered", "missing (have it)", "missing (gap)"]

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_FANCY_DASH = re.compile(r"[–—]")
_PRIORITIES = (
    ("required_skills", "required"),
    ("preferred_skills", "preferred"),
    ("keywords", "keyword"),
    ("company_keywords", "company"),
)
_COL = re.compile(br'w:num="(\d+)"')


def pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def docx_text(docx_bytes: bytes) -> str:
    document = Document(io.BytesIO(docx_bytes))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def ats_report(
    *,
    pdf_bytes: bytes,
    docx_bytes: bytes,
    resume: dict[str, Any],
    master: dict[str, Any],
    keywords: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """What an ATS reads from the PDF and the DOCX, and which terms it finds in both."""
    pdf = pdf_text(pdf_bytes)
    docx = docx_text(docx_bytes)
    no_tables, no_images, one_column = _docx_shape(docx_bytes)
    personal = resume.get("personalInfo") or {}
    email = str(personal.get("email", "")).strip()
    phone = str(personal.get("phone", "")).strip()
    both = f"{pdf}\n{docx}"
    checks = {
        "text_extracted": bool(pdf.strip()),
        "docx_text_extracted": bool(docx.strip()),
        "email_as_text": (not email) or email.casefold() in both.casefold() or bool(_EMAIL.search(both)),
        "phone_as_text": (not phone) or phone in both or bool(_PHONE.search(both)),
        "ascii_dates": _FANCY_DASH.search(both) is None,
        "no_cid_garbage": "(cid:" not in both and "�" not in both,
        "no_tables": no_tables,
        "no_images": no_images,
        "one_column": one_column,
        "standard_headings": _headings_present(resume, pdf, docx),
    }
    pdf_flat = " ".join(pdf.split())
    docx_flat = " ".join(docx.split())
    coverage = coverage_for(keywords, pdf_text=pdf_flat, docx_text=docx_flat, master_text=all_text(master))
    covered = sum(row["status"] == "covered" for row in coverage)
    return {
        "extractor": "pypdf+docx",
        "checks": checks,
        "keyword_match": round(covered * 100 / len(coverage)) if coverage else 0,
        "coverage": coverage,
        "warnings": warnings,
        "passed": all(checks.values()),
    }


def coverage_for(
    keywords: dict[str, Any], *, pdf_text: str, docx_text: str, master_text: str
) -> list[dict[str, str]]:
    priority: dict[str, str] = {}
    for field, label in _PRIORITIES:
        for term in keywords.get(field) or []:
            priority.setdefault(term, label)
    source = _sources(keywords)
    return [
        {
            "term": term,
            "priority": priority.get(term, "company"),
            "source": source.get(term)
            or source.get(term.casefold())
            or ("company" if priority.get(term) == "company" else "advert"),
            "status": _status(term, pdf_text, docx_text, master_text),
        }
        for term in job_keyword_terms(keywords)
    ]


def _sources(keywords: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in keywords.get("sources") or []:
        if not isinstance(item, dict) or not item.get("term"):
            continue
        term = str(item["term"])
        label = str(item.get("source") or "advert")
        found[term] = label
        found.setdefault(term.casefold(), label)
    return found


def _status(term: str, pdf_text: str, docx_text: str, master_text: str) -> Coverage:
    if term_in_text(term, pdf_text) and term_in_text(term, docx_text):
        return "covered"
    if term_in_text(term, master_text):
        return "missing (have it)"
    return "missing (gap)"


def _docx_shape(docx_bytes: bytes) -> tuple[bool, bool, bool]:
    document = Document(io.BytesIO(docx_bytes))
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        xml = archive.read("word/document.xml")
        images = any(name.startswith("word/media/") for name in archive.namelist())
    numbers = [int(item) for item in _COL.findall(xml)]
    return len(document.tables) == 0, not images, all(number <= 1 for number in numbers)


def _headings_present(resume: dict[str, Any], pdf: str, docx: str) -> bool:
    headings = [text for kind, text in blocks(resume) if kind == "section"]
    return all(heading in pdf and heading in docx for heading in headings)
