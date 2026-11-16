"""Prepare applications and list their status for the logged-in person."""

from __future__ import annotations

import html
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.api.jobs import get_gateway
from app.applications.extension_device import issue_token
from app.applications.ui import applied_button
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    mark_submitted,
    prepare_application,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import Applicant, Application, ApplicationStatus, Job, JobFreshness

router = APIRouter(tags=["applications"])
_CSRF = [Depends(require_csrf)]
_SEE_OTHER = status.HTTP_303_SEE_OTHER
_STATUS_LABEL = {
    ApplicationStatus.preparing.value: "Preparing",
    ApplicationStatus.ready_to_fill.value: "Ready to fill",
    ApplicationStatus.filled_awaiting_review.value: "Filled — review before submit",
    ApplicationStatus.submitted.value: "Applied",
    ApplicationStatus.needs_input.value: "Needs your answers",
    ApplicationStatus.failed.value: "Failed",
    ApplicationStatus.abandoned.value: "Abandoned",
}


def _e(value: object) -> str:
    return html.escape(str(value))


def _card(request: Request, app_row: Application, job: Job | None) -> str:
    title = job.title if job else "Job"
    company = job.company_name if job else ""
    status_label = _STATUS_LABEL.get(app_row.status, app_row.status)
    missing = ""
    if app_row.missing_fields:
        items = "".join(f"<li>{_e(m)}</li>" for m in app_row.missing_fields)
        missing = (
            f'<p class="hint">Missing required answers:</p><ul>{items}</ul>'
            '<p><a href="/profile/apply-answers">Update apply answers</a></p>'
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
    if app_row.status != ApplicationStatus.submitted.value:
        actions.append(applied_button(csrf_field(request), app_row.job_id))
    return f"""
<article class="card">
  <h2>{_e(title)}</h2>
  <p class="muted">{_e(company)} · {_e(status_label)}</p>
  {error}{missing}
  <div class="job-foot">{"".join(actions)}</div>
</article>
"""


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
    banner = (
        f'<p class="notice">{awaiting} application(s) filled and waiting for your review before submit.</p>'
        if awaiting
        else ""
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
async def prepare(
    job_id: uuid.UUID = Form(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> RedirectResponse:
    try:
        row = await prepare_application(
            db,
            applicant=applicant,
            job_id=job_id,
            gateway=gateway,
            settings=get_settings(),
            persist=db.commit,
        )
        db.commit()
    except PrepareRefused as exc:
        db.rollback()
        return RedirectResponse(f"/listings?apply_error={exc.message}", status_code=_SEE_OTHER)
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
    job = db.get(Job, job_id)
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    mark_submitted(db, applicant.id, job)
    db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=_SEE_OTHER)


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
