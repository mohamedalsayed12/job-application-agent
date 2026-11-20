"""Prepare applications and list their status for the logged-in person."""

from __future__ import annotations

import html
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.api.listings import visible_job
from app.applicants.apply_answers import (
    MAX_SAVED,
    AnswersRefused,
    SavedQuestion,
    save_question_answers,
)
from app.applications.extension_device import issue_token
from app.applications.prepare import (
    PrepareRefused,
    application_for_job,
    get_application,
    list_applications,
    mark_abandoned,
    mark_submitted,
    prepare_application,
    store_fill_outcome,
)
from app.applications.ui import abandon_button, applied_button, prepare_button
from app.core.db import get_db
from app.models import Applicant, Application, ApplicationStatus, Job, JobFreshness

router = APIRouter(tags=["applications"])
_CSRF = [Depends(require_csrf)]
_SEE_OTHER = status.HTTP_303_SEE_OTHER
_STATUS_LABEL = {
    ApplicationStatus.ready_to_fill.value: "Ready to fill",
    ApplicationStatus.filled_awaiting_review.value: "Filled — review before submit",
    ApplicationStatus.submitted.value: "Applied",
    ApplicationStatus.needs_input.value: "Needs your answers",
    ApplicationStatus.failed.value: "Failed",
    ApplicationStatus.abandoned.value: "Abandoned",
}


_RETRY_LABEL = {
    ApplicationStatus.needs_input.value: "Check again",
    ApplicationStatus.failed.value: "Try again",
    ApplicationStatus.abandoned.value: "Prepare apply",
}


def _e(value: object) -> str:
    return html.escape(str(value))


def _card(request: Request, app_row: Application, job: Job | None) -> str:
    title = job.title if job else "Job"
    company = job.company_name if job else ""
    status_label = _STATUS_LABEL.get(app_row.status, app_row.status)
    missing = ""
    if app_row.status == ApplicationStatus.needs_input.value and app_row.missing_fields:
        fields = []
        for index, question in enumerate(app_row.missing_fields):
            fields.append(
                f"<label>{_e(question)}"
                f'<input type="hidden" name="question_{index}" value="{_e(question)}"/>'
                f'<textarea name="answer_{index}" required maxlength="2000"></textarea></label>'
            )
        missing = (
            '<form method="post" action="/applications/'
            f'{_e(app_row.id)}/answers">{csrf_field(request)}{"".join(fields)}'
            '<button type="submit" class="primary">Save answers</button></form>'
        )
    error = f'<p class="error">{_e(app_row.error)}</p>' if app_row.error else ""
    actions = [
        f'<a class="button" href="{_e(app_row.apply_url)}" rel="noopener noreferrer" target="_blank">Open apply page</a>'
    ]
    if app_row.status == ApplicationStatus.ready_to_fill.value:
        actions.append(
            f'<form method="post" action="/applications/{app_row.id}/open-fill" class="inline">'
            f'{csrf_field(request)}<button type="submit" class="primary">Open &amp; fill</button></form>'
        )
    if app_row.status == ApplicationStatus.filled_awaiting_review.value:
        actions.append('<span class="tag good">Ready for your review — submit yourself</span>')
    csrf = csrf_field(request)
    retry_label = _RETRY_LABEL.get(app_row.status)
    if retry_label:
        actions.append(prepare_button(csrf, app_row.job_id, retry_label))
    if app_row.status != ApplicationStatus.submitted.value:
        actions.append(applied_button(csrf, app_row.job_id))
    if app_row.status not in (ApplicationStatus.submitted.value, ApplicationStatus.abandoned.value):
        actions.append(abandon_button(csrf, app_row.job_id))
    return f"""
<article class="card">
  <h2>{_e(title)}</h2>
  <p class="muted">{_e(company)} · {_e(status_label)}</p>
  {error}{missing}
  <div class="job-foot">{"".join(actions)}</div>
</article>
"""


def _back_to(job: Job | None) -> str:
    """The job page exists only while the job is open; a closed job's application lives on the list."""
    return f"/jobs/{job.id}" if job is not None and job.freshness == JobFreshness.open else "/applications"


@router.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = list_applications(db, applicant.id)
    cards = [_card(request, row, db.get(Job, row.job_id)) for row in rows]
    body = "".join(cards) or '<p class="muted">No applications prepared yet. Use Prepare apply on a listing.</p>'
    awaiting = sum(1 for r in rows if r.status == ApplicationStatus.filled_awaiting_review.value)
    questions = [name for r in rows if r.status == ApplicationStatus.needs_input.value for name in r.missing_fields]
    notice = request.query_params.get("notice")
    banner = f'<p class="error">{_e(notice)}</p>' if notice else ""
    if questions:
        banner += "<p class=\"notice\">Answer these questions, then fill again: " + ", ".join(_e(name) for name in questions) + "</p>"
    if awaiting:
        banner += (
            f'<p class="notice">{awaiting} application(s) filled and waiting for your review before submit.</p>'
        )
    return page(
        "Applications",
        f"""
<div class="page-head"><div>
  <h1>Applications</h1>
  <p class="muted">The extension fills the form in your browser and stops before Submit.
  Set <a href="/profile/apply-answers">apply answers</a> and an <a href="/account#extension">extension token</a> first.</p>
</div></div>
{banner}
{body}
""",
        header=applicant_bar(request, "applications"),
        narrow=True,
    )


@router.post("/applications", dependencies=_CSRF)
def prepare(
    job_id: uuid.UUID = Form(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    visible_job(db, applicant.id, job_id)
    try:
        row = prepare_application(db, applicant_id=applicant.id, job_id=job_id)
        db.commit()
    except PrepareRefused as exc:
        db.rollback()
        job = db.get(Job, job_id)
        where = f"/jobs/{job_id}" if job is not None and job.freshness == JobFreshness.open else "/applications"
        return RedirectResponse(f"{where}?notice={quote(exc.message)}", status_code=_SEE_OTHER)
    return RedirectResponse(f"/applications/{row.id}", status_code=_SEE_OTHER)


@router.get("/applications/{application_id}", response_class=HTMLResponse)
def application_detail(
    application_id: uuid.UUID,
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    row = get_application(db, application_id, applicant.id)
    if row is None:
        return page("Not found", "<h1>Not found</h1>", status_code=404, header=applicant_bar(request, "applications"))
    return page(
        "Application",
        f'<p><a href="/applications">All applications</a></p>{_card(request, row, db.get(Job, row.job_id))}',
        header=applicant_bar(request, "applications"),
        narrow=True,
    )


@router.post("/applications/{application_id}/answers", dependencies=_CSRF)
async def save_missing_answers(
    application_id: uuid.UUID,
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    row = get_application(db, application_id, applicant.id)
    if row is None or row.status != ApplicationStatus.needs_input.value:
        return RedirectResponse("/applications", status_code=_SEE_OTHER)
    form = await request.form()
    pairs: list[SavedQuestion] = []
    index = 0
    while f"question_{index}" in form and index < MAX_SAVED:
        pairs.append(SavedQuestion(str(form.get(f"question_{index}") or ""), str(form.get(f"answer_{index}") or "")))
        index += 1
    try:
        save_question_answers(db, applicant.id, pairs)
    except AnswersRefused as exc:
        db.rollback()
        return RedirectResponse(f"/applications/{application_id}?answer_error={exc.message}", status_code=_SEE_OTHER)
    store_fill_outcome(row, None, reopen=True)
    db.commit()
    return RedirectResponse(f"/applications/{application_id}", status_code=_SEE_OTHER)


@router.post("/applications/{application_id}/open-fill", dependencies=_CSRF)
def open_fill(
    application_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    row = get_application(db, application_id, applicant.id)
    if row is None or row.status != ApplicationStatus.ready_to_fill.value:
        return RedirectResponse("/applications", status_code=_SEE_OTHER)
    return RedirectResponse(row.apply_url, status_code=_SEE_OTHER)


@router.post("/jobs/{job_id}/applied", dependencies=_CSRF)
def applied(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if application_for_job(db, applicant.id, job_id) is not None:
        # An application already made can be marked applied even after the job closes.
        job = db.get(Job, job_id)
    else:
        job = visible_job(db, applicant.id, job_id)
    mark_submitted(db, applicant.id, job)
    db.commit()
    return RedirectResponse(_back_to(job), status_code=_SEE_OTHER)


@router.post("/jobs/{job_id}/abandoned", dependencies=_CSRF)
def abandoned(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    row = mark_abandoned(db, applicant.id, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    db.commit()
    return RedirectResponse(_back_to(db.get(Job, job_id)), status_code=_SEE_OTHER)


@router.post("/account/extension-token", dependencies=_CSRF)
def rotate_extension_token(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    token = issue_token(db, applicant.id)
    db.commit()
    return page(
        "Extension token",
        f"""
<div class="card">
  <h1>Extension token</h1>
  <p>Paste this into the Job Agent extension. It is shown once.</p>
  <p><code id="ext-token">{_e(token)}</code></p>
  <p class="hint">Rotating this invalidates the previous token.</p>
  <p><a href="/account">Back to account</a></p>
</div>
""",
        header=applicant_bar(request, "account"),
        narrow=True,
    )
