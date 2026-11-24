"""Apply-answers profile: every answer the sign-up wizard asked, on one page to change any time."""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.answer_fields import (
    answers_from_form,
    availability_fields,
    diversity_fields,
    esc,
    extra_fields,
    extra_keys,
    yes_no_select,
)
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.applicants.apply_answers import (
    ANSWER_FIELDS,
    AnswersInput,
    AnswersRefused,
    input_from_row,
    load_answers,
    save_answers,
)
from app.applicants.questions import GROUP_TITLES
from app.core.db import get_db
from app.models import Applicant

router = APIRouter(prefix="/profile/apply-answers", tags=["apply-answers"])
_CSRF = [Depends(require_csrf)]
_SEE_OTHER = status.HTTP_303_SEE_OTHER
# The old free-text "how did you hear" answer is kept only as a fallback; the extra questions ask it as a choice.
_FIELDS = tuple(name for name in ANSWER_FIELDS if name != "heard_about")
_EXTRAS = extra_keys(*GROUP_TITLES)


def _group(answers: AnswersInput, group: str) -> str:
    return f'<section class="card"><h2>{esc(GROUP_TITLES[group])}</h2>{extra_fields(answers, group)}</section>'


def _page(request: Request, answers: AnswersInput, *, message: str = "", status_code: int = 200) -> HTMLResponse:
    return page(
        "Apply answers",
        f"""
<div class="page-head"><div>
  <h1>Answers for applications</h1>
  <p class="muted">Used only to fill employer forms in your browser. Nothing is submitted for you.</p>
</div></div>
{message}
<form method="post" action="/profile/apply-answers">
  {csrf_field(request)}
  <section class="card">
    <h2>Contact</h2>
    <div class="row">
      <label>Legal first name <input name="legal_first_name" value="{esc(answers.legal_first_name)}"/></label>
      <label>Legal last name <input name="legal_last_name" value="{esc(answers.legal_last_name)}"/></label>
    </div>
    <label>Phone <input name="phone" value="{esc(answers.phone)}"/></label>
    <label>Address <input name="address_line" value="{esc(answers.address_line)}"/></label>
    <div class="row">
      <label>City <input name="city" value="{esc(answers.city)}"/></label>
      <label>Postcode <input name="postcode" value="{esc(answers.postcode)}"/></label>
      <label>Country <input name="country" value="{esc(answers.country or 'United Kingdom')}"/></label>
    </div>
    <label>LinkedIn URL <input name="linkedin_url" value="{esc(answers.linkedin_url)}"/></label>
    <label>GitHub URL <input name="github_url" value="{esc(answers.github_url)}"/></label>
    <label>Portfolio URL <input name="portfolio_url" value="{esc(answers.portfolio_url)}"/></label>
  </section>
  {_group(answers, "details")}
  <section class="card">
    <h2>Work authorisation</h2>
    <div class="row">
      <label>Right to work in the UK {yes_no_select("right_to_work_uk", answers.right_to_work_uk)}</label>
      <label>Need visa sponsorship {yes_no_select("needs_visa_sponsorship", answers.needs_visa_sponsorship)}</label>
    </div>
    <label>Nationality <input name="nationality" value="{esc(answers.nationality)}"/></label>
    {extra_fields(answers, "right_to_work")}
    <label>Previously employed by the company {yes_no_select("previously_employed", answers.previously_employed or "no")}</label>
  </section>
  {_group(answers, "education")}
  {_group(answers, "work")}
  <section class="card">
    <h2>Availability</h2>
    {availability_fields(answers, required=False)}
  </section>
  {_group(answers, "screening")}
  <section class="card">
    <h2>Optional diversity questions</h2>
    <p class="hint">Many UK employers ask these to monitor fairness. Every one defaults to prefer not to say,
    and none is ever guessed from your CV.</p>
    {diversity_fields(answers)}
    {extra_fields(answers, "diversity")}
  </section>
  <div class="form-foot">
    <a class="button" href="/profile">Back to profile</a>
    <button type="submit" class="primary">Save apply answers</button>
  </div>
</form>
""",
        status_code=status_code,
        header=applicant_bar(request, "profile"),
        narrow=True,
    )


@router.get("", response_class=HTMLResponse)
def show_answers(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> HTMLResponse:
    return _page(request, input_from_row(load_answers(db, applicant.id)))


@router.post("", response_model=None, dependencies=_CSRF)
async def save(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    answers = answers_from_form(form, input_from_row(load_answers(db, applicant.id)), _FIELDS, _EXTRAS)
    try:
        save_answers(db, applicant.id, answers)
        db.commit()
    except AnswersRefused as exc:
        db.rollback()
        return _page(request, answers, message=f'<p class="error">{html.escape(exc.message)}</p>', status_code=400)
    return RedirectResponse("/profile/apply-answers", status_code=_SEE_OTHER)
