"""The apply step on a job's page: prepare, review, then the person says they applied."""

from __future__ import annotations

import html
import uuid

from app.models import Application, ApplicationStatus, Job


def applied_button(csrf: str, job_id: uuid.UUID) -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/applied" class="inline">'
        f'{csrf}<button type="submit" title="Press after you have submitted the application on the employer&#39;s site">'
        "Submitted</button></form>"
    )


def prepare_button(csrf: str, job_id: uuid.UUID, label: str, *, primary: bool = True) -> str:
    cls = ' class="primary"' if primary else ""
    return (
        f'<form method="post" action="/applications" class="inline">{csrf}'
        f'<input type="hidden" name="job_id" value="{html.escape(str(job_id))}"/>'
        f'<button type="submit"{cls}>{html.escape(label)}</button></form> '
    )


def abandon_button(csrf: str, job_id: uuid.UUID) -> str:
    return (
        f'<form method="post" action="/jobs/{html.escape(str(job_id))}/abandoned" class="inline">'
        f'{csrf}<button type="submit">I&#39;m not applying</button></form>'
    )


def _usable(url: str | None) -> bool:
    """Only web links: an apply URL comes from outside data."""
    return bool(url) and url.lower().startswith(("https://", "http://"))


def apply_step(job: Job, application: Application | None, csrf: str) -> str:
    if application is not None and application.status == ApplicationStatus.submitted.value:
        return (
            "<p><b>You applied for this job.</b></p>"
            f'<p><a class="button" href="/jobs/{html.escape(str(job.id))}/outreach">Reach out to people at this company</a></p>'
        )
    posting = (
        f'<a class="button" href="{html.escape(job.apply_url, quote=True)}" rel="noopener noreferrer">Original posting</a> '
        if _usable(job.apply_url)
        else ""
    )
    if application is None:
        prepare = (
            prepare_button(csrf, job.id, "Prepare apply")
            if _usable(job.apply_url)
            else "<span>No usable apply link was given.</span> "
        )
        return f"<p>{prepare}{posting}{applied_button(csrf, job.id)}</p>"
    detail = f'<a href="/applications/{html.escape(str(application.id))}">Your application</a>'
    status = application.status
    retry = ""
    if status == ApplicationStatus.filled_awaiting_review.value:
        note = "The form is filled in your browser. Check the answers, press Submit on the employer's page yourself, then tell us here."
    elif status == ApplicationStatus.needs_input.value:
        note = (
            "Answer these before the form can be filled: "
            + "; ".join(application.missing_fields or ["more answers"])
            + ". Add them on your apply answers page, then check again."
        )
        retry = prepare_button(csrf, job.id, "Check again")
    elif status == ApplicationStatus.failed.value:
        note = f"Preparing this application failed: {application.error or 'the form could not be read'}."
        retry = prepare_button(csrf, job.id, "Try again")
    elif status == ApplicationStatus.abandoned.value:
        note = "You marked this as not applying."
        retry = prepare_button(csrf, job.id, "Prepare apply")
    elif status == ApplicationStatus.ready_to_fill.value:
        note = "This application is ready to fill."
    else:
        note = "This application is being prepared."
    abandon = "" if status == ApplicationStatus.abandoned.value else abandon_button(csrf, job.id)
    return f"<p>{html.escape(note)} {detail}</p><p>{retry}{posting}{applied_button(csrf, job.id)} {abandon}</p>"
