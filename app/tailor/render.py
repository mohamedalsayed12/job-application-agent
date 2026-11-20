"""Render a structured CV, and its cover letter, to ATS-friendly single-column PDF and DOCX bytes."""

from __future__ import annotations

import io
from typing import Any, Literal

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate

Kind = Literal["name", "muted", "section", "body", "bullet"]

BULLET = "•"
# Helvetica writes the bullet as WinAnsi code 0x7F, which pypdf extracts as a DEL
# character. An embedded TrueType font carries a ToUnicode map, so it extracts as "•".
_BULLET_FONT = "Vera"
pdfmetrics.registerFont(TTFont(_BULLET_FONT, "Vera.ttf"))

_ADDITIONAL = (
    ("languages", "Languages"),
    ("certificationsTraining", "Certifications"),
    ("awards", "Awards"),
)


def blocks(resume: dict[str, Any]) -> list[tuple[Kind, str]]:
    """The CV as an ordered list of styled lines, shared by both formats."""
    out: list[tuple[Kind, str]] = []
    personal = resume.get("personalInfo") or {}
    if personal.get("name"):
        out.append(("name", personal["name"]))
    for fields in (("title",), ("email", "phone", "location"), ("website", "linkedin", "github")):
        line = _join(personal.get(field) for field in fields)
        if line:
            out.append(("muted", line))
    if resume.get("summary"):
        out += [("section", "Profile"), ("body", resume["summary"])]
    additional = resume.get("additional") or {}
    if additional.get("technicalSkills"):
        out += [("section", "Skills"), ("body", ", ".join(additional["technicalSkills"]))]
    for section, heading, fields in (
        ("workExperience", "Experience", ("title", "company", "location", "years")),
        ("personalProjects", "Projects", ("name", "role", "years")),
    ):
        if resume.get(section):
            out.append(("section", heading))
            for entry in resume[section]:
                out.append(("body", _join(entry.get(field) for field in fields)))
                out += [("bullet", row) for row in entry.get("description") or []]
    if resume.get("education"):
        out.append(("section", "Education"))
        for entry in resume["education"]:
            out.append(("body", _join(entry.get(field) for field in ("degree", "institution", "years"))))
            if entry.get("description"):
                out.append(("muted", entry["description"]))
    lists = [(label, additional[field]) for field, label in _ADDITIONAL if additional.get(field)]
    if lists:
        out.append(("section", "Additional"))
        out += [("body", f"{label}: {', '.join(items)}") for label, items in lists]
    return out


def render_pdf(resume: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    personal = resume.get("personalInfo") or {}
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=personal.get("name") or "CV",
    )
    sheet = getSampleStyleSheet()
    body = ParagraphStyle("CvBody", parent=sheet["Normal"], fontSize=10, leading=13)
    styles = {
        "name": ParagraphStyle("CvName", parent=sheet["Heading1"], fontSize=16, spaceAfter=4),
        "section": ParagraphStyle("CvSection", parent=sheet["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4),
        "body": body,
        "muted": ParagraphStyle("CvMuted", parent=body, textColor="#333333"),
    }
    story: list = []
    bullets: list = []
    for kind, text in blocks(resume):
        if kind == "bullet":
            bullets.append(ListItem(Paragraph(_xml(text), body), leftIndent=8))
            continue
        if bullets:
            story.append(_bullet_list(bullets))
            bullets = []
        story.append(Paragraph(_xml(text), styles[kind]))
    if bullets:
        story.append(_bullet_list(bullets))
    doc.build(story)
    return buffer.getvalue()


def render_docx(resume: dict[str, Any]) -> bytes:
    document = Document()
    for kind, text in blocks(resume):
        if kind == "name":
            document.add_heading(text, level=1)
        elif kind == "section":
            document.add_heading(text, level=2)
        elif kind == "bullet":
            document.add_paragraph(text, style="List Bullet")
        else:
            document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def letter_blocks(letter: str, resume: dict[str, Any]) -> list[tuple[Kind, str]]:
    """A contact header from the tailored CV, then one block per paragraph of the letter."""
    personal = resume.get("personalInfo") or {}
    out: list[tuple[Kind, str]] = []
    if personal.get("name"):
        out.append(("name", personal["name"]))
    contact = _join(personal.get(field) for field in ("email", "phone", "location"))
    if contact:
        out.append(("muted", contact))
    out += [("body", part) for part in _paragraphs(letter)]
    return out


def render_letter_pdf(letter: str, resume: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="Cover letter",
    )
    sheet = getSampleStyleSheet()
    body = ParagraphStyle("LetterBody", parent=sheet["Normal"], fontSize=11, leading=15, spaceAfter=9)
    styles = {
        "name": ParagraphStyle("LetterName", parent=sheet["Heading1"], fontSize=16, spaceAfter=2),
        "muted": ParagraphStyle("LetterMuted", parent=body, fontSize=10, textColor="#333333", spaceAfter=16),
        "body": body,
    }
    doc.build(
        [Paragraph(_xml(text).replace("\n", "<br/>"), styles[kind]) for kind, text in letter_blocks(letter, resume)]
    )
    return buffer.getvalue()


def render_letter_docx(letter: str, resume: dict[str, Any]) -> bytes:
    document = Document()
    for kind, text in letter_blocks(letter, resume):
        if kind == "name":
            document.add_heading(text, level=1)
        else:
            document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraphs(letter: str) -> list[str]:
    return [part.strip() for part in letter.replace("\r\n", "\n").split("\n\n") if part.strip()]


def _bullet_list(items: list) -> ListFlowable:
    return ListFlowable(items, bulletType="bullet", start=BULLET, bulletFontName=_BULLET_FONT)


def _join(parts) -> str:
    return " · ".join(str(part).strip() for part in parts if part and str(part).strip())


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
