"""Shared HTML for tailored-CV status on listings and the job page."""

from __future__ import annotations

import html
import uuid

from app.models import TailoredCv, TailoredCvStatus

_FLAGGED = "posting_flagged"
_SKIPPED_TEXT = {
    "no_description": "Tailored CV skipped — this job has no description to tailor against.",
}
_SKIPPED_DEFAULT = "Tailored CV skipped — upload a readable CV on your profile."


def _reason(row: TailoredCv) -> str | None:
    return row.draft.get("reason") if isinstance(row.draft, dict) else None


def tailor_button(csrf: str, job_id: uuid.UUID, *, label: str = "Tailor my CV for this job") -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/tailor" class="inline">'
        f'{csrf}<button type="submit">{html.escape(label)}</button></form>'
    )


def tailor_section(job_id: uuid.UUID, row: TailoredCv | None, csrf: str, *, allowed: bool) -> str:
    """The job page's tailored-CV area: status, plus the button when a request would do something."""
    status = tailored_block(job_id, row)
    if not allowed:
        return status
    if row is None:
        return f"<p>{tailor_button(csrf, job_id)}</p>"
    if row.status == TailoredCvStatus.failed.value and _reason(row) != _FLAGGED:
        return f"{status}<p>{tailor_button(csrf, job_id, label='Try again')}</p>"
    if row.status == TailoredCvStatus.pending.value:
        return '<p class="muted">Tailoring your CV for this job. Refresh in a few minutes.</p>'
    return status


def tailored_block(job_id: uuid.UUID, row: TailoredCv | None, *, verdict: str | None = None) -> str:
    if row is None:
        if verdict in ("Apply", "Consider"):
            return '<p class="muted">Tailored CV pending.</p>'
        return ""
    if row.status == TailoredCvStatus.waiting_budget.value:
        return '<p class="muted">Tailored CV waiting on the monthly budget.</p>'
    if row.status == TailoredCvStatus.skipped.value:
        return f'<p class="muted">{_SKIPPED_TEXT.get(_reason(row), _SKIPPED_DEFAULT)}</p>'
    if row.status == TailoredCvStatus.failed.value:
        if _reason(row) == _FLAGGED:
            return '<p class="muted">Tailored CV not built: this advert was flagged as unsafe to tailor.</p>'
        return '<p class="muted">Tailored CV could not be built for this posting.</p>'
    if row.status != TailoredCvStatus.ready.value:
        return '<p class="muted">Tailored CV pending.</p>'
    report = row.ats_report or {}
    coverage = report.get("coverage") or []
    gaps = sum(1 for item in coverage if item.get("status") == "missing (gap)")
    checks = report.get("checks") or {}
    layer_ok = all(checks.values()) if checks else False
    if layer_ok and gaps == 0:
        ats = "ATS text layer and keywords look good."
    elif layer_ok:
        ats = f"ATS text layer ok; {gaps} keyword gap(s)."
    else:
        ats = "ATS checks need a look."
    match = f"Keyword match {report['keyword_match']}%." if coverage and "keyword_match" in report else ""
    jid = html.escape(str(job_id))
    return (
        f"<p><b>Tailored CV ready.</b> {html.escape(ats)} {html.escape(match)} "
        f'<a href="/jobs/{jid}/tailored.pdf">PDF</a> · '
        f'<a href="/jobs/{jid}/tailored.docx">DOCX</a></p>'
    )
