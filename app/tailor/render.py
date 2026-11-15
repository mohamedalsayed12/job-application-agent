"""Render a grounded structured CV to ATS-friendly PDF and DOCX bytes."""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer


def render_pdf(draft: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=_contact(draft).get("name") or "CV",
    )
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle("CvName", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    section = ParagraphStyle("CvSection", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("CvBody", parent=styles["Normal"], fontSize=10, leading=13)
    muted = ParagraphStyle("CvMuted", parent=body, textColor="#333333")

    story: list = []
    contact = _contact(draft)
    if contact.get("name"):
        story.append(Paragraph(_xml(contact["name"]), name_style))
    line = " · ".join(
        part for part in (contact.get("email"), contact.get("phone"), contact.get("location")) if part
    )
    if line:
        story.append(Paragraph(_xml(line), muted))
    if contact.get("links"):
        story.append(Paragraph(_xml(" · ".join(contact["links"])), muted))
    story.append(Spacer(1, 4 * mm))

    if draft.get("summary"):
        story.append(Paragraph("Profile", section))
        story.append(Paragraph(_xml(draft["summary"]), body))

    if draft.get("skills"):
        story.append(Paragraph("Skills", section))
        story.append(Paragraph(_xml(", ".join(draft["skills"])), body))

    if draft.get("experience"):
        story.append(Paragraph("Experience", section))
        for job in draft["experience"]:
            dates = " - ".join(part for part in (job.get("start"), job.get("end")) if part)
            head = " · ".join(part for part in (job.get("title"), job.get("employer"), dates) if part)
            story.append(Paragraph(_xml(head), body))
            bullets = [
                ListItem(Paragraph(_xml(bullet), body), leftIndent=8, value="bullet")
                for bullet in job.get("bullets") or []
            ]
            if bullets:
                story.append(ListFlowable(bullets, bulletType="bullet", start="bullet"))

    if draft.get("education"):
        story.append(Paragraph("Education", section))
        for edu in draft["education"]:
            dates = " - ".join(part for part in (edu.get("start"), edu.get("end")) if part)
            head = " · ".join(part for part in (edu.get("degree"), edu.get("school"), dates) if part)
            story.append(Paragraph(_xml(head), body))
            if edu.get("details"):
                story.append(Paragraph(_xml(edu["details"]), muted))

    doc.build(story)
    return buffer.getvalue()


def render_docx(draft: dict[str, Any]) -> bytes:
    document = Document()
    contact = _contact(draft)
    if contact.get("name"):
        document.add_heading(contact["name"], level=1)
    line = " · ".join(
        part for part in (contact.get("email"), contact.get("phone"), contact.get("location")) if part
    )
    if line:
        document.add_paragraph(line)
    if contact.get("links"):
        document.add_paragraph(" · ".join(contact["links"]))

    if draft.get("summary"):
        document.add_heading("Profile", level=2)
        document.add_paragraph(draft["summary"])

    if draft.get("skills"):
        document.add_heading("Skills", level=2)
        document.add_paragraph(", ".join(draft["skills"]))

    if draft.get("experience"):
        document.add_heading("Experience", level=2)
        for job in draft["experience"]:
            dates = " - ".join(part for part in (job.get("start"), job.get("end")) if part)
            head = " · ".join(part for part in (job.get("title"), job.get("employer"), dates) if part)
            document.add_paragraph(head)
            for bullet in job.get("bullets") or []:
                document.add_paragraph(bullet, style="List Bullet")

    if draft.get("education"):
        document.add_heading("Education", level=2)
        for edu in draft["education"]:
            dates = " - ".join(part for part in (edu.get("start"), edu.get("end")) if part)
            head = " · ".join(part for part in (edu.get("degree"), edu.get("school"), dates) if part)
            document.add_paragraph(head)
            if edu.get("details"):
                document.add_paragraph(edu["details"])

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _contact(draft: dict[str, Any]) -> dict[str, Any]:
    raw = draft.get("contact")
    return raw if isinstance(raw, dict) else {}


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
