"""Shared HTML for tailored-CV status on listings and the job page."""

from __future__ import annotations

import html
import uuid

from app.models import TailoredCv, TailoredCvStatus


def tailor_button(csrf: str, job_id: uuid.UUID, *, label: str = "Tailor my CV for this job") -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/tailor" class="inline">'
        f'{csrf}<button type="submit">{html.escape(label)}</button></form>'
    )


def tailor_section(job_id: uuid.UUID, row: TailoredCv | None, csrf: str, *, allowed: bool) -> str:
    """The job page's tailored-CV area: status, plus the button when a request would do something."""
    status = tailored_block(job_id, row)
    if row is not None and row.status == TailoredCvStatus.ready.value:
        status += changes_block(row.ats_report or {})
    if row is not None and row.status == TailoredCvStatus.ready.value and row.cover_letter:
        status += (
            "<details><summary>Read the cover letter</summary>"
            f'<pre style="white-space: pre-wrap">{html.escape(row.cover_letter)}</pre></details>'
        )
    if not allowed:
        return status
    if row is None:
        return f"<p>{tailor_button(csrf, job_id)}</p>"
    if row.status == TailoredCvStatus.failed.value:
        return f"{status}<p>{tailor_button(csrf, job_id, label='Try again')}</p>"
    if row.status == TailoredCvStatus.pending.value:
        return '<p class="muted">Tailoring your CV for this job. Refresh in a few minutes.</p>'
    return status


def changes_block(report: dict) -> str:
    """What the tailoring reworded, beside the person's own wording, and what they could do next (D75)."""
    out = ""
    changes = report.get("changes")
    if changes is not None:
        if not changes:
            out += '<p class="muted">No wording changed: your CV already matched this advert.</p>'
        else:
            rows = "".join(
                f"<li><b>{html.escape(item.get('where', ''))}</b><br>"
                f'<span class="muted">Before:</span> <del>{html.escape(item.get("before", ""))}</del><br>'
                f'<span class="muted">After:</span> {html.escape(item.get("after", ""))}</li>'
                for item in changes
            )
            noun = "change" if len(changes) == 1 else "changes"
            out += (
                f"<details><summary>See what changed ({len(changes)} {noun})</summary>"
                "<p class=\"muted\">Everything else is your own wording. Edit the downloaded DOCX if a line "
                f"doesn't sound like you.</p><ul>{rows}</ul></details>"
            )
    advice = report.get("recommendations") or []
    if advice:
        items = "".join(f"<li>{html.escape(line)}</li>" for line in advice)
        out += f"<details><summary>Recommendations ({len(advice)})</summary><ul>{items}</ul></details>"
    return out


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
    letter = (
        f' Cover letter: <a href="/jobs/{jid}/cover-letter.pdf">PDF</a> · '
        f'<a href="/jobs/{jid}/cover-letter.docx">DOCX</a>'
        if row.cover_letter
        else ""
    )
    return (
        f"<p><b>Tailored CV ready.</b> {html.escape(ats)} {html.escape(match)} "
        f'<a href="/jobs/{jid}/tailored.pdf">PDF</a> · '
        f'<a href="/jobs/{jid}/tailored.docx">DOCX</a>.{letter}</p>'
    )
