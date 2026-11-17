"""Shared HTML for tailored-CV status on listings and the job page."""

from __future__ import annotations

import html
import uuid

from app.models import TailoredCv, TailoredCvStatus, attempt_failed

_REASONS = {
    "posting_flagged": "The posting looked unsafe, so no CV was written.",
    "cv_not_parsed": "The CV could not be read into sections.",
    "too_large": "The file was too large.",
    "no_cv": "No CV is uploaded.",
    "unreadable": "The CV could not be read.",
}
# Unfinished packages. With no automatic verdict or request behind one, the worker never picks it up.
_UNFINISHED = frozenset(
    {TailoredCvStatus.pending.value, TailoredCvStatus.running.value, TailoredCvStatus.waiting_budget.value}
)
_SOURCE = {
    "advert": "the advert",
    "company": "the company website",
    "advert and company": "the advert and the company website",
}


def tailor_button(csrf: str, job_id: uuid.UUID, *, label: str = "Tailor my CV for this job") -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/tailor" class="inline">'
        f'{csrf}<button type="submit">{html.escape(label)}</button></form>'
    )


def listing_tailor(
    job_id: uuid.UUID,
    row: TailoredCv | None,
    csrf: str,
    *,
    auto: bool,
    allowed: bool,
) -> str:
    """Status for a job tailored with no click (`auto`, see `auto_tailored`), or the one-click button for any other job."""
    due = auto or (row is not None and row.requested_at is not None)
    if not due and (row is None or row.status in _UNFINISHED):
        return f"<p>{tailor_button(csrf, job_id)}</p>" if allowed else ""
    if row is None and not allowed:
        return ""
    block = tailored_block(job_id, row, auto=auto)
    retry = row is not None and (
        row.status == TailoredCvStatus.failed.value or attempt_failed(row.status, row.draft)
    )
    if allowed and retry:
        return f"{block}<p>{tailor_button(csrf, job_id, label='Try again')}</p>"
    return block


def tailored_block(job_id: uuid.UUID, row: TailoredCv | None, *, auto: bool) -> str:
    if row is None:
        if auto:
            return '<p class="muted">Tailored CV queued.</p>'
        return ""
    status = row.status
    if status in {TailoredCvStatus.pending.value, TailoredCvStatus.running.value}:
        label = "in progress" if status == TailoredCvStatus.running.value else "queued"
        return f'<p class="muted">Tailored CV {label}.</p>'
    if status == TailoredCvStatus.waiting_budget.value:
        return '<p class="muted">Tailored CV waiting on the monthly budget.</p>'
    if status == TailoredCvStatus.skipped.value:
        return f'<p class="muted">Tailored CV skipped. {html.escape(_reason(row))}</p>'
    if status == TailoredCvStatus.failed.value:
        return f'<p class="muted">Tailored CV failed. {html.escape(_reason(row))}</p>'
    if status != TailoredCvStatus.ready.value:
        return '<p class="muted">Tailored CV queued.</p>'
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
    retry_note = ""
    if attempt_failed(status, row.draft):
        retry_note = (
            f'<p class="muted">The latest attempt failed. {html.escape(_reason(row))} '
            "The previous file is still here.</p>"
        )
    return (
        f"<p><b>Tailored CV ready.</b> {html.escape(ats)} {html.escape(match)} "
        f'<a href="/jobs/{jid}/tailored.pdf">PDF</a> · '
        f'<a href="/jobs/{jid}/tailored.docx">DOCX</a></p>'
        f"{retry_note}{_company_note(row)}{_coverage(row)}"
    )


def _reason(row: TailoredCv) -> str:
    raw = ""
    if isinstance(row.draft, dict):
        raw = str(row.draft.get("reason") or row.draft.get("last_error") or "")
    if raw in _REASONS:
        return _REASONS[raw]
    if raw and len(raw) < 160 and "<" not in raw:
        return raw
    return "The CV could not be built."


def _company_note(row: TailoredCv) -> str:
    if row.company_research_available:
        return ""
    note = ""
    if isinstance(row.keywords, dict):
        note = str(row.keywords.get("company_note") or "")
    if not note:
        note = "No employer website was named in the advert or the apply link."
    return f'<p class="muted">Company website: {html.escape(note)}</p>'


def _coverage(row: TailoredCv) -> str:
    coverage = (row.ats_report or {}).get("coverage") or []
    if not coverage:
        return ""
    items = []
    for item in coverage:
        term = str(item.get("term") or "")
        source = str(item.get("source") or ("company" if item.get("priority") == "company" else "advert"))
        label = _SOURCE.get(source, source)
        items.append(
            f"<li>{html.escape(term)} "
            f'<span class="muted">from {html.escape(label)}, {html.escape(str(item.get("status") or ""))}</span></li>'
        )
    return '<ul class="plain">' + "".join(items) + "</ul>"
