"""The apply step on a job's page: prepare, review, then the person says they applied."""

from __future__ import annotations

import html
import uuid

from app.models import Application, ApplicationStatus, Job


def applied_button(csrf: str, job_id: uuid.UUID) -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/applied" class="inline">'
        f'{csrf}<button type="submit">I&#39;ve applied</button></form>'
    )


def _usable(url: str | None) -> bool:
    """Only web links: an apply URL comes from outside data."""
    return bool(url) and url.lower().startswith(("https://", "http://"))


def apply_step(job: Job, application: Application | None, csrf: str) -> str:
    if application is not None and application.status == ApplicationStatus.submitted.value:
        return "<p><b>You applied for this job.</b></p>"
    posting = (
        f'<a class="button" href="{html.escape(job.apply_url, quote=True)}" rel="noopener noreferrer">Original posting</a> '
        if _usable(job.apply_url)
        else ""
    )
    if application is None:
        prepare = (
            f'<form method="post" action="/applications" class="inline">{csrf}'
            f'<input type="hidden" name="job_id" value="{html.escape(str(job.id))}"/>'
            '<button type="submit" class="primary">Prepare apply</button></form> '
            if _usable(job.apply_url)
            else "<span>No usable apply link was given.</span> "
        )
        return f"<p>{prepare}{posting}{applied_button(csrf, job.id)}</p>"
    detail = f'<a href="/applications/{html.escape(str(application.id))}">Your application</a>'
    if application.status == ApplicationStatus.filled_awaiting_review.value:
        note = "The form is filled in your browser. Check the answers, press Submit on the employer's page yourself, then tell us here."
    else:
        note = "This application is in progress."
    return f"<p>{html.escape(note)} {detail}</p><p>{posting}{applied_button(csrf, job.id)}</p>"
