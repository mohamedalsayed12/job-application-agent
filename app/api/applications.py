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
from sqlalchemy import select

from app.applicants.apply_answers import (
    MAX_SAVED,
    AnswersRefused,
    SavedQuestion,
    question_key,
    save_question_answers,
)
from app.applications.drafts import answer_question
from app.applications.extension_device import issue_token
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    mark_submitted,
    prepare_application,
    store_fill_outcome,
)
from app.applications.ui import applied_button
from app.core.db import get_db
from app.models import Applicant, Application, ApplicationStatus, FormQuestion, Job, JobFreshness
from app.outreach.auto import drafts_waiting

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


def _e(value: object) -> str:
    return html.escape(str(value))


# The extension can still fill these; a filled one may have more pages to go.
_FILLABLE = (
    ApplicationStatus.ready_to_fill.value,
    ApplicationStatus.needs_input.value,
    ApplicationStatus.filled_awaiting_review.value,
)


def question_form(request: Request, row: FormQuestion, back: str) -> str:
    """One banked question with a box for the answer, reused on every form that asks it (D75)."""
    if row.options:
        options = "".join(
            f'<option value="{_e(o)}"{" selected" if o == row.answer else ""}>{_e(o)}</option>' for o in row.options
        )
        control = f'<select name="answer"><option value="">Not answered</option>{options}</select>'
    elif row.field_type == "textarea":
        control = f'<textarea name="answer" rows="4">{_e(row.answer or "")}</textarea>'
    else:
        control = f'<input name="answer" value="{_e(row.answer or "")}"/>'
    return (
        f'<form method="post" action="/form-questions/{row.id}" class="question">{csrf_field(request)}'
        f'<input type="hidden" name="back" value="{_e(back)}"/>'
        f'<label>{_e(row.label)} {control}</label><button type="submit">Save answer</button></form>'
    )


def _drafts(app_row: Application) -> str:
    """Every page's checked drafts for this application."""
    steps = [s for s in ((app_row.fill_plan_json or {}).get("drafts") or {}).values() if s and s.get("value")]
    if not steps:
        return ""
    items = "".join(
        f'<li><b>{_e(s.get("label"))}</b><p>{_e(s.get("value"))}</p><p class="hint">{_e(s.get("note"))}</p></li>'
        for s in steps
    )
    return (
        '<details class="drafts"><summary>Answers the AI drafted and checked for this form '
        f'({len(steps)})</summary><p class="hint">They are highlighted on the form. Change anything that is not '
        f'how you would say it before you submit.</p><ul>{items}</ul></details>'
    )


def _questions(db: Session, applicant_id: uuid.UUID) -> dict[str, FormQuestion]:
    rows = db.scalars(select(FormQuestion).where(FormQuestion.applicant_id == applicant_id))
    return {row.question_key: row for row in rows}


def _card(
    request: Request, app_row: Application, job: Job | None, questions: dict[str, FormQuestion] | None = None,
    waiting: int = 0,
) -> str:
    title = job.title if job else "Job"
    company = job.company_name if job else ""
    status_label = _STATUS_LABEL.get(app_row.status, app_row.status)
    missing = ""
    if app_row.missing_fields:
        bank = questions or {}
        answerable = [bank[k] for m in app_row.missing_fields if (k := question_key(m)) in bank]
        plain = [m for m in app_row.missing_fields if question_key(m) not in bank]
        items = "".join(f"<li>{_e(m)}</li>" for m in plain)
        forms = "".join(question_form(request, row, f"/applications/{app_row.id}") for row in answerable)
        if not answerable and plain:
            fields = []
            for index, question in enumerate(plain):
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
        else:
            missing = (
                '<p class="hint">This form asks questions you have no saved answer for. Answer them here once and '
                "every form that asks the same question is filled from then on, or answer them on the employer&#39;s page:</p>"
                f"{forms}{f'<ul>{items}</ul>' if items else ''}"
                '<p><a href="/profile/apply-answers">Update apply answers</a></p>'
            )
    how = ""
    if app_row.status in _FILLABLE:
        how = (
            '<p class="hint">Press <b>Open &amp; fill</b>. The Job Agent extension fills each page of the '
            "employer&#39;s form as it opens, from your saved answers and CV. Questions they don&#39;t cover are "
            "drafted from your CV and checked, then outlined in orange for you to read. It never presses Submit: "
            "you check the form and submit it yourself. If a page is not filled, press <b>Fill this tab</b> "
            "in the extension.</p>"
        )
    # When unanswered questions are listed below, repeating them in error is noise, not a failure.
    error = (
        f'<p class="error">{_e(app_row.error)}</p>'
        if app_row.error and not app_row.missing_fields
        else ""
    )
    actions = [
        f'<a class="button" href="{_e(app_row.apply_url)}" rel="noopener noreferrer" target="_blank">Open apply page</a>'
    ]
    if app_row.status in _FILLABLE:
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
  {how}{error}{missing}{_drafts(app_row)}{_outreach(app_row, waiting)}
  <div class="job-foot">{"".join(actions)}</div>
</article>
"""


def _outreach(app_row: Application, waiting: int) -> str:
    """After Submitted: where the people and drafts are (D77)."""
    if app_row.status != ApplicationStatus.submitted.value or app_row.outreach_state is None:
        return ""
    link = f'<a href="/jobs/{_e(str(app_row.job_id))}/outreach">'
    if app_row.outreach_state == "queued":
        return (
            '<p class="hint">Finding the hiring manager and people in this role, and drafting a message to each. '
            "This takes a few minutes; nothing is sent without you.</p>"
        )
    if app_row.outreach_state == "no_contacts":
        return f'<p class="hint">No one at this company was found. {link}Look again or add someone</a>.</p>'
    if waiting:
        return (
            f'<p class="notice">{waiting} outreach draft(s) ready. {link}Read and send them</a>: '
            "Gmail sends in one click, and the extension types LinkedIn notes for you to send.</p>"
        )
    return f'<p class="hint">{link}Outreach for this job</a></p>'


@router.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = list_applications(db, applicant.id)
    bank = _questions(db, applicant.id)
    cards = [_card(request, row, db.get(Job, row.job_id), bank, drafts_waiting(db, row)) for row in rows]
    body = "".join(cards) or '<p class="muted">No applications prepared yet. Use Prepare apply on a listing.</p>'
    awaiting = sum(1 for r in rows if r.status == ApplicationStatus.filled_awaiting_review.value)
    questions = [name for r in rows if r.status == ApplicationStatus.needs_input.value for name in r.missing_fields]
    banner = ""
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
  <p class="muted">The extension fills each page of the form in your browser, drafts open questions from your CV,
  and stops before Submit. Set <a href="/profile/apply-answers">apply answers</a> and an
  <a href="/account#extension">extension token</a> first.</p>
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
    try:
        row = prepare_application(db, applicant=applicant, job_id=job_id)
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
    card = _card(request, row, db.get(Job, row.job_id), _questions(db, applicant.id))
    return page(
        "Application",
        f'<p><a href="/applications">All applications</a></p>{card}',
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
    if row is None or row.status not in _FILLABLE:
        return RedirectResponse("/applications", status_code=_SEE_OTHER)
    return RedirectResponse(row.apply_url, status_code=_SEE_OTHER)




@router.post("/form-questions/{question_id}", dependencies=_CSRF)
def save_question(
    question_id: uuid.UUID,
    answer: str = Form(""),
    back: str = Form("/applications"),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """The person's answer to a question a form asked, used on every form that asks it again."""
    try:
        row = answer_question(db, applicant.id, question_id, answer)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"{back}?answer_error={exc}", status_code=_SEE_OTHER)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Reopen every application that was waiting on this question.
    for app_row in list_applications(db, applicant.id):
        if app_row.status == ApplicationStatus.needs_input.value and any(
            question_key(label) == row.question_key for label in app_row.missing_fields
        ):
            store_fill_outcome(app_row, None, reopen=True)
    db.commit()
    target = back if back.startswith("/") else "/applications"
    return RedirectResponse(target, status_code=_SEE_OTHER)


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
