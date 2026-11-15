"""Shared HTML for tailored-CV status on listings and the job page."""

from __future__ import annotations

import html
import uuid

from app.models import TailoredCv, TailoredCvStatus


def tailored_block(job_id: uuid.UUID, row: TailoredCv | None, *, verdict: str | None = None) -> str:
    if row is None:
        if verdict in ("Apply", "Consider"):
            return '<p class="muted">Tailored CV pending.</p>'
        return ""
    if row.status == TailoredCvStatus.waiting_budget.value:
        return '<p class="muted">Tailored CV waiting on the monthly budget.</p>'
    if row.status == TailoredCvStatus.skipped.value:
        return '<p class="muted">Tailored CV skipped — upload a readable CV on your profile.</p>'
    if row.status == TailoredCvStatus.failed.value:
        return '<p class="muted">Tailored CV could not be built for this posting.</p>'
    if row.status != TailoredCvStatus.ready.value:
        return '<p class="muted">Tailored CV pending.</p>'
    research = (
        "Company research used."
        if row.company_research_available
        else "Company research unavailable; keywords and CV only."
    )
    coverage = (row.ats_report or {}).get("coverage") or []
    gaps = sum(1 for item in coverage if item.get("status") == "missing (gap)")
    checks = (row.ats_report or {}).get("checks") or {}
    layer_ok = all(checks.values()) if checks else False
    if layer_ok and gaps == 0:
        ats = "ATS text layer and keywords look good."
    elif layer_ok:
        ats = f"ATS text layer ok; {gaps} keyword gap(s)."
    else:
        ats = "ATS checks need a look."
    jid = html.escape(str(job_id))
    return (
        f"<p><b>Tailored CV ready.</b> {html.escape(research)} {html.escape(ats)} "
        f'<a href="/jobs/{jid}/tailored.pdf">PDF</a> · '
        f'<a href="/jobs/{jid}/tailored.docx">DOCX</a></p>'
    )
