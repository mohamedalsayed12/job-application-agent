"""Apply-answers profile: contact, work auth, optional demographics."""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.applicants.apply_answers import AnswersInput, AnswersRefused, load_answers, save_answers
from app.core.db import get_db
from app.models import PREFER_NOT, Applicant

router = APIRouter(prefix="/profile/apply-answers", tags=["apply-answers"])
_CSRF = [Depends(require_csrf)]
_SEE_OTHER = status.HTTP_303_SEE_OTHER


def _opt(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _bool_select(name: str, current: bool | None) -> str:
    chosen = "unset" if current is None else ("yes" if current else "no")
    options = (("unset", "Not set"), ("yes", "Yes"), ("no", "No"))
    body = "".join(
        f'<option value="{val}"{" selected" if val == chosen else ""}>{lab}</option>' for val, lab in options
    )
    return f'<select name="{name}">{body}</select>'


def _page(request: Request, applicant: Applicant, db: Session, *, message: str = "", status_code: int = 200) -> HTMLResponse:
    row = load_answers(db, applicant.id)
    token = csrf_field(request)
    return page(
        "Apply answers",
        f"""
<div class="page-head"><div>
  <h1>Answers for applications</h1>
  <p class="muted">Used only to fill employer forms in your browser. Demographics default to prefer not to say.
  Nothing is submitted for you.</p>
</div></div>
{message}
<form method="post" action="/profile/apply-answers">
  {token}
  <section class="card">
    <h2>Contact</h2>
    <div class="row">
      <label>Legal first name <input name="legal_first_name" value="{_opt(row.legal_first_name if row else '')}"/></label>
      <label>Legal last name <input name="legal_last_name" value="{_opt(row.legal_last_name if row else '')}"/></label>
    </div>
    <label>Phone <input name="phone" value="{_opt(row.phone if row else '')}"/></label>
    <label>Address <input name="address_line" value="{_opt(row.address_line if row else '')}"/></label>
    <div class="row">
      <label>City <input name="city" value="{_opt(row.city if row else '')}"/></label>
      <label>Postcode <input name="postcode" value="{_opt(row.postcode if row else '')}"/></label>
      <label>Country <input name="country" value="{_opt((row.country if row else None) or 'United Kingdom')}"/></label>
    </div>
    <label>LinkedIn URL <input name="linkedin_url" value="{_opt(row.linkedin_url if row else '')}"/></label>
    <label>GitHub URL <input name="github_url" value="{_opt(row.github_url if row else '')}"/></label>
    <label>Portfolio URL <input name="portfolio_url" value="{_opt(row.portfolio_url if row else '')}"/></label>
  </section>
  <section class="card">
    <h2>Work authorisation</h2>
    <div class="row">
      <label>Right to work in the UK {_bool_select("right_to_work_uk", row.right_to_work_uk if row else None)}</label>
      <label>Need visa sponsorship {_bool_select("needs_visa_sponsorship", row.needs_visa_sponsorship if row else None)}</label>
    </div>
    <label>Nationality <input name="nationality" value="{_opt(row.nationality if row else '')}"/></label>
    <label>How you usually heard about the role <input name="heard_about" value="{_opt((row.heard_about if row else None) or 'Job board')}"/></label>
    <label>Previously employed by the company {_bool_select("previously_employed", row.previously_employed if row else False)}</label>
  </section>
  <section class="card">
    <h2>Optional demographics</h2>
    <p class="hint">These default to prefer not to say. They are never inferred from your CV.</p>
    <label>Free school meals / socio-economic
      <input name="free_school_meals" value="{_opt((row.free_school_meals if row else None) or PREFER_NOT)}"/>
    </label>
    <label>Gender <input name="gender" value="{_opt((row.gender if row else None) or PREFER_NOT)}"/></label>
    <label>Ethnicity <input name="ethnicity" value="{_opt((row.ethnicity if row else None) or PREFER_NOT)}"/></label>
    <label>Disability <input name="disability" value="{_opt((row.disability if row else None) or PREFER_NOT)}"/></label>
    <label>Veteran status <input name="veteran" value="{_opt((row.veteran if row else None) or PREFER_NOT)}"/></label>
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
    return _page(request, applicant, db)


@router.post("", response_class=HTMLResponse, dependencies=_CSRF)
def save(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    legal_first_name: str = Form(""),
    legal_last_name: str = Form(""),
    phone: str = Form(""),
    address_line: str = Form(""),
    city: str = Form(""),
    postcode: str = Form(""),
    country: str = Form(""),
    linkedin_url: str = Form(""),
    github_url: str = Form(""),
    portfolio_url: str = Form(""),
    right_to_work_uk: str = Form("unset"),
    needs_visa_sponsorship: str = Form("unset"),
    nationality: str = Form(""),
    heard_about: str = Form(""),
    previously_employed: str = Form("no"),
    free_school_meals: str = Form(PREFER_NOT),
    gender: str = Form(PREFER_NOT),
    ethnicity: str = Form(PREFER_NOT),
    disability: str = Form(PREFER_NOT),
    veteran: str = Form(PREFER_NOT),
) -> HTMLResponse:
    try:
        save_answers(
            db,
            applicant.id,
            AnswersInput(
                legal_first_name=legal_first_name,
                legal_last_name=legal_last_name,
                phone=phone,
                address_line=address_line,
                city=city,
                postcode=postcode,
                country=country,
                linkedin_url=linkedin_url,
                github_url=github_url,
                portfolio_url=portfolio_url,
                right_to_work_uk=right_to_work_uk,
                needs_visa_sponsorship=needs_visa_sponsorship,
                nationality=nationality,
                heard_about=heard_about,
                previously_employed=previously_employed,
                free_school_meals=free_school_meals,
                gender=gender,
                ethnicity=ethnicity,
                disability=disability,
                veteran=veteran,
            ),
        )
        db.commit()
    except AnswersRefused as exc:
        db.rollback()
        return _page(
            request,
            applicant,
            db,
            message=f'<p class="error">{html.escape(exc.message)}</p>',
            status_code=400,
        )
    return RedirectResponse("/profile/apply-answers", status_code=_SEE_OTHER)
