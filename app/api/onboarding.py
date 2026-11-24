"""The sign-up wizard: CV, details read from it, job preferences, education and work, availability, diversity.

Each step saves when the person presses Continue, so leaving halfway loses nothing: logging in
again opens the step they reached. What the CV read suggests is kept on the CV row only until the
person has checked it, and every suggestion is copied from the CV text (D43), never guessed.
"""

from __future__ import annotations

import html
from dataclasses import asdict, replace
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.accounts import (
    after_login_url,
    current_applicant,
    register_form,
    require_login,
)
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
from app.api.chrome import onboarding_bar
from app.api.forms import csrf_field, require_csrf
from app.api.jobs import get_gateway
from app.api.profile import (
    PROFILE_STYLE,
    READ_PROBLEMS,
    SKILL_SEARCH_SCRIPT,
    ProfileFields,
    fill_from_cv,
    limit_cv_upload,
    preference_sections,
    read_cv_upload,
    save_profile_fields,
    saved_fields,
    size_text,
)
from app.applicants.apply_answers import (
    AnswersInput,
    AnswersRefused,
    input_from_row,
    load_answers,
    save_answers,
)
from app.applicants.cv import MAX_CV_BYTES, CvRefused, cv_for, store_cv
from app.applicants.cv_read import (
    CvSuggestions,
    ReadOutcome,
    read_cv,
    suggestions_from_json,
    suggestions_to_json,
)
from app.applicants.service import PreferenceValidationError
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.matching.match import match_all
from app.models import (
    Applicant,
    ApplicantPreference,
    ApplicantProfile,
    CvFileType,
    OnboardingStep,
)

router = APIRouter(tags=["onboarding"])

_SEE_OTHER = status.HTTP_303_SEE_OTHER
_CSRF = [Depends(require_csrf)]
_STEPS = list(OnboardingStep)
# The account itself is step 1, so the wizard's own steps are numbered from 2.
_TITLES = {
    OnboardingStep.cv: "Your CV",
    OnboardingStep.details: "Your details",
    OnboardingStep.preferences: "Jobs you want",
    OnboardingStep.education: "Education and work",
    OnboardingStep.availability: "Availability",
    OnboardingStep.diversity: "Diversity questions",
}
_DETAIL_FIELDS = (
    "legal_first_name",
    "legal_last_name",
    "phone",
    "address_line",
    "city",
    "postcode",
    "country",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "right_to_work_uk",
    "needs_visa_sponsorship",
    "nationality",
)
_DETAIL_EXTRAS = extra_keys("details", "right_to_work")
_EDUCATION_EXTRAS = extra_keys("education", "work")
_AVAILABILITY_FIELDS = ("notice_period", "earliest_start_date", "willing_to_relocate", "work_hours", "work_location")
_AVAILABILITY_EXTRAS = extra_keys("screening")
_DIVERSITY_EXTRAS = extra_keys("diversity")
_DIVERSITY_FIELDS = (
    "gender",
    "ethnicity",
    "disability",
    "veteran",
    "free_school_meals",
    "sexual_orientation",
    "religion",
    "caring_responsibilities",
    "school_type",
    "parental_occupation",
)
# Contact details the CV can fill: the answer field, and the CV suggestion it comes from.
_FROM_CV = {
    "legal_first_name": "first_name",
    "legal_last_name": "last_name",
    "phone": "phone",
    "city": "city",
    "postcode": "postcode",
    "linkedin_url": "linkedin_url",
    "github_url": "github_url",
    "portfolio_url": "portfolio_url",
}
_READ_NOTICES = {
    ReadOutcome.read.value: None,
    **{outcome.value: message for outcome, message in READ_PROBLEMS.items()},
}

_WIZARD_STYLE = """<style>
.steps { display: flex; gap: 0.35rem; list-style: none; padding: 0; margin: 0 0 1.25rem; flex-wrap: wrap; }
.steps li { flex: 1 1 5.5rem; font-size: 0.78rem; color: var(--muted); border-top: 3px solid var(--line); padding-top: 0.35rem; }
.steps li.done { border-color: var(--primary); }
.steps li.now { border-color: var(--primary); color: var(--text); font-weight: 600; }
.steps a { color: inherit; text-decoration: none; }
.from-cv { font-size: 0.75rem; color: var(--primary); font-weight: 600; }
.wizard-foot { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-top: 1.25rem; }
</style>"""


def _index(step: OnboardingStep) -> int:
    return _STEPS.index(step)


def _reached(applicant: Applicant) -> OnboardingStep | None:
    return OnboardingStep(applicant.onboarding_step) if applicant.onboarding_step else None


def _next_url(applicant: Applicant) -> str:
    reached = _reached(applicant)
    return f"/onboarding/{reached.value}" if reached else "/listings"


def _guard(applicant: Applicant, step: OnboardingStep) -> RedirectResponse | None:
    """Earlier steps can be revisited; later ones open only once reached. A finished wizard sends people on."""
    reached = _reached(applicant)
    if reached is None or _index(step) > _index(reached):
        return RedirectResponse(_next_url(applicant), status_code=_SEE_OTHER)
    return None


def _advance(applicant: Applicant, done: OnboardingStep) -> str:
    """Moves the person past `done` (never backwards) and returns the step to show next."""
    reached = _reached(applicant)
    following = _STEPS[_index(done) + 1] if _index(done) + 1 < len(_STEPS) else None
    if reached is not None and following is not None and _index(following) > _index(reached):
        applicant.onboarding_step = following.value
    return f"/onboarding/{following.value}" if following else "/listings"


def _progress(applicant: Applicant, step: OnboardingStep) -> str:
    reached = _reached(applicant) or _STEPS[-1]
    items = ['<li class="done">1. Account</li>']
    for number, item in enumerate(_STEPS, start=2):
        title = f"{number}. {_TITLES[item]}"
        if item == step:
            items.append(f'<li class="now" aria-current="step">{title}</li>')
        elif _index(item) <= _index(reached):
            items.append(f'<li class="done"><a href="/onboarding/{item.value}">{title}</a></li>')
        else:
            items.append(f"<li>{title}</li>")
    return f'<ol class="steps">{"".join(items)}</ol>'


def _wizard_page(
    request: Request,
    applicant: Applicant,
    step: OnboardingStep,
    body: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    if notice:
        message += f'<div class="notice">{html.escape(notice)}</div>'
    number = _index(step) + 2
    return page(
        f"Sign up: {_TITLES[step]}",
        f"""{PROFILE_STYLE}{_WIZARD_STYLE}
{_progress(applicant, step)}
<p class="muted">Step {number} of {len(_STEPS) + 1}</p>
{message}
{body}""",
        status_code=status_code,
        header=onboarding_bar(request),
        narrow=True,
    )


def _back(step: OnboardingStep) -> str:
    if _index(step) == 0:
        return "<span></span>"
    previous = _STEPS[_index(step) - 1]
    return f'<a class="button" href="/onboarding/{previous.value}">Back</a>'


def _suggestions(db: Session, applicant: Applicant) -> CvSuggestions | None:
    cv = cv_for(db, applicant.id)
    return suggestions_from_json(cv.suggestions) if cv is not None else None


# Home and the wizard's entry point.


@router.get("/", response_model=None)
def home(request: Request, applicant: Applicant | None = Depends(current_applicant)) -> Response:
    """Visitors see the sign-up form first, with a link to log in. People logged in go where they left off."""
    if applicant is not None:
        target = _next_url(applicant) if applicant.email_verified_at else after_login_url(applicant)
        return RedirectResponse(target, status_code=_SEE_OTHER)
    return register_form(request)


@router.get("/onboarding")
def onboarding(applicant: Applicant = Depends(require_login)) -> RedirectResponse:
    return RedirectResponse(_next_url(applicant), status_code=_SEE_OTHER)


# Step 2: CV.


def _cv_body(request: Request, db: Session, applicant: Applicant) -> str:
    cv = cv_for(db, applicant.id)
    current = ""
    if cv is not None:
        current = f"""
<div class="cv-current">
  <span>Your CV: <a href="/profile/cv">cv.{esc(cv.file_type)}</a>, {size_text(cv.size_bytes)}, uploaded {cv.uploaded_at:%d %B %Y}.</span>
  <form method="post" action="/onboarding/cv/keep">{csrf_field(request)}<button type="submit" class="primary">Continue with this CV</button></form>
</div>"""
    return f"""
<section class="card">
<h1>Upload your CV</h1>
<p class="muted">We read it to fill in the next steps for you, then use it to tailor applications. You check
everything before it's saved, and we only copy what your CV actually says.</p>
{current}
<form method="post" action="/onboarding/cv" enctype="multipart/form-data" class="dropzone">
  {csrf_field(request)}
  <p><b>{"Upload a different CV" if cv else "Choose your CV"}</b></p>
  <p class="hint">PDF or Word (.docx), up to {MAX_CV_BYTES // (1024 * 1024)} MB.</p>
  <input name="cv" type="file" required aria-label="CV file"
    accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"/>
  <p><button type="submit" class="primary">Upload and continue</button></p>
</form>
</section>
<div class="wizard-foot">{_back(OnboardingStep.cv)}</div>
"""


@router.get("/onboarding/cv", response_model=None)
def cv_step(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.cv):
        return redirect
    return _wizard_page(request, applicant, OnboardingStep.cv, _cv_body(request, db, applicant))


@router.post(
    "/onboarding/cv",
    response_model=None,
    dependencies=[Depends(require_login), Depends(limit_cv_upload), Depends(require_csrf)],
)
async def cv_step_upload(
    request: Request,
    content: bytes | None = Depends(read_cv_upload),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    if redirect := _guard(applicant, OnboardingStep.cv):
        return redirect
    try:
        cv = store_cv(db, applicant.id, content)
        db.commit()
    except CvRefused as exc:
        db.rollback()
        return _wizard_page(
            request, applicant, OnboardingStep.cv, _cv_body(request, db, applicant), error=str(exc), status_code=400
        )
    read = await read_cv(db, applicant.id, content, CvFileType(cv.file_type), gateway=gateway, settings=get_settings())
    if read.suggestions is not None:
        cv.suggestions = suggestions_to_json(read.suggestions)
    next_url = _advance(applicant, OnboardingStep.cv)
    db.commit()
    return RedirectResponse(f"{next_url}?cv={read.outcome.value}", status_code=_SEE_OTHER)


@router.post("/onboarding/cv/keep", response_model=None, dependencies=_CSRF)
def cv_step_keep(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.cv):
        return redirect
    if cv_for(db, applicant.id) is None:
        return _wizard_page(
            request, applicant, OnboardingStep.cv, _cv_body(request, db, applicant), error="Upload your CV first.", status_code=400
        )
    next_url = _advance(applicant, OnboardingStep.cv)
    db.commit()
    return RedirectResponse(next_url, status_code=_SEE_OTHER)


# Step 3: details for employer forms.


def _prefilled_details(saved: AnswersInput, found: CvSuggestions | None) -> tuple[AnswersInput, set[str]]:
    """Saved answers win. A blank one takes the CV's value, and is marked so the person checks it."""
    if found is None:
        return saved, set()
    filled: dict[str, str] = {}
    for answer, suggestion in _FROM_CV.items():
        value = getattr(found.contact, suggestion)
        if value and not getattr(saved, answer).strip():
            filled[answer] = value
    return replace(saved, **filled), set(filled)


def _details_body(request: Request, answers: AnswersInput, from_cv: set[str]) -> str:
    def tag(name: str) -> str:
        return ' <span class="from-cv">from your CV</span>' if name in from_cv else ""

    def text(name: str, label: str, *, required: bool = False, kind: str = "text", hint: str = "") -> str:
        extra = f' <span class="hint">{hint}</span>' if hint else ""
        return (
            f'<label>{label}{tag(name)}{extra} <input name="{name}" type="{kind}" value="{esc(getattr(answers, name))}"'
            f'{" required" if required else ""}/></label>'
        )

    return f"""
<form method="post" action="/onboarding/details">
  {csrf_field(request)}
  <section class="card">
    <h1>Your details</h1>
    <p class="muted">Employers ask for these on almost every application. We use them only to fill in forms in
    your own browser, and we never submit an application for you.</p>
    <div class="row">
      {text("legal_first_name", "Legal first name", required=True)}
      {text("legal_last_name", "Legal last name", required=True)}
    </div>
    {text("phone", "Phone", required=True, kind="tel")}
    {text("address_line", "Address", hint="optional")}
    <div class="row">
      {text("city", "Town or city", required=True)}
      {text("postcode", "Postcode", hint="optional")}
      <label>Country <input name="country" value="{esc(answers.country or 'United Kingdom')}"/></label>
    </div>
    {text("linkedin_url", "LinkedIn profile", kind="url", hint="optional")}
    {text("github_url", "GitHub profile", kind="url", hint="optional")}
    {text("portfolio_url", "Portfolio or personal website", kind="url", hint="optional")}
  </section>
  <section class="card">
    <h2>More about you</h2>
    <p class="hint">Optional. Some forms ask these, so answering once saves typing them each time.</p>
    {extra_fields(answers, "details")}
  </section>
  <section class="card">
    <h2>Right to work</h2>
    <p class="hint">We never work these out from your CV. Answer them yourself.</p>
    <div class="row">
      <label>Do you have the right to work in the UK? {yes_no_select("right_to_work_uk", answers.right_to_work_uk, required=True)}</label>
      <label>Will you need visa sponsorship? {yes_no_select("needs_visa_sponsorship", answers.needs_visa_sponsorship, required=True)}</label>
    </div>
    {text("nationality", "Nationality", hint="optional")}
    {extra_fields(answers, "right_to_work")}
  </section>
  <div class="wizard-foot">{_back(OnboardingStep.details)}<button type="submit" class="primary">Continue</button></div>
</form>
"""


def _required_details(answers: AnswersInput) -> None:
    """The form marks these required; this catches a browser that ignores that."""
    if not (answers.legal_first_name.strip() and answers.legal_last_name.strip()):
        raise AnswersRefused("Enter your legal first and last name.")
    if not answers.phone.strip():
        raise AnswersRefused("Enter your phone number.")
    if not answers.city.strip():
        raise AnswersRefused("Enter your town or city.")
    if answers.right_to_work_uk not in ("yes", "no") or answers.needs_visa_sponsorship not in ("yes", "no"):
        raise AnswersRefused("Answer both right-to-work questions.")
    if not answers.extra.get("right_to_work_type", "").strip():
        raise AnswersRefused("Choose what gives you the right to work in the UK.")


@router.get("/onboarding/details", response_model=None)
def details_step(
    request: Request,
    cv: str = Query(default=""),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    if redirect := _guard(applicant, OnboardingStep.details):
        return redirect
    row = load_answers(db, applicant.id)
    # Once the person has saved this step, a field they cleared stays clear.
    found = _suggestions(db, applicant) if row is None else None
    answers, from_cv = _prefilled_details(input_from_row(row), found)
    notice = _READ_NOTICES.get(cv)
    if cv == ReadOutcome.read.value:
        notice = (
            "We read your CV and filled in what it shows. Check each answer marked 'from your CV'."
            if from_cv
            else "We read your CV but it doesn't show your contact details. Fill them in below."
        )
    return _wizard_page(request, applicant, OnboardingStep.details, _details_body(request, answers, from_cv), notice=notice)


@router.post("/onboarding/details", response_model=None, dependencies=_CSRF)
async def details_save(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.details):
        return redirect
    answers = answers_from_form(
        await request.form(), input_from_row(load_answers(db, applicant.id)), _DETAIL_FIELDS, _DETAIL_EXTRAS
    )
    try:
        _required_details(answers)
        save_answers(db, applicant.id, answers)
    except AnswersRefused as exc:
        db.rollback()
        return _wizard_page(
            request,
            applicant,
            OnboardingStep.details,
            _details_body(request, answers, set()),
            error=exc.message,
            status_code=400,
        )
    next_url = _advance(applicant, OnboardingStep.details)
    db.commit()
    return RedirectResponse(next_url, status_code=_SEE_OTHER)


# Step 4: the jobs they want (the same fields as the profile page).


def _preferences_body(request: Request, fields: ProfileFields) -> str:
    return f"""
<h1>Jobs you want</h1>
<p class="muted">We search for jobs with these and score each one against your CV.</p>
<form method="post" action="/onboarding/preferences" id="profile-form">
  {csrf_field(request)}
  {preference_sections(fields, first_number=1)}
  <div class="wizard-foot">{_back(OnboardingStep.preferences)}<button type="submit" class="primary">Continue</button></div>
</form>
{SKILL_SEARCH_SCRIPT}
"""


@router.get("/onboarding/preferences", response_model=None)
def preferences_step(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.preferences):
        return redirect
    fields = saved_fields(db, applicant)
    notice = None
    if (found := _suggestions(db, applicant)) is not None:
        fields, filled = fill_from_cv(fields, found)
        if filled:
            notice = f"From your CV we filled in {', '.join(filled)}. Check them and change anything that's wrong."
    return _wizard_page(request, applicant, OnboardingStep.preferences, _preferences_body(request, fields), notice=notice)


@router.post("/onboarding/preferences", response_model=None, dependencies=_CSRF)
def preferences_save(
    request: Request,
    fields: ProfileFields = Depends(),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    if redirect := _guard(applicant, OnboardingStep.preferences):
        return redirect
    try:
        save_profile_fields(db, applicant, fields)
    except PreferenceValidationError as exc:
        db.rollback()
        return _wizard_page(
            request, applicant, OnboardingStep.preferences, _preferences_body(request, fields), error=str(exc), status_code=400
        )
    next_url = _advance(applicant, OnboardingStep.preferences)
    db.commit()
    return RedirectResponse(next_url, status_code=_SEE_OTHER)


# Step 5: education and work.


def _prefilled_education(db: Session, applicant: Applicant, saved: AnswersInput) -> tuple[AnswersInput, set[str]]:
    """Until any of this step is saved, blanks take the CV's education and job and the profile's degree and salary.

    Returns the answers and the keys copied from the CV, which are marked so the person checks them.
    """
    if any(key in saved.extra for key in _EDUCATION_EXTRAS):
        return saved, set()
    filled: dict[str, str] = {}
    from_cv: set[str] = set()
    if (found := _suggestions(db, applicant)) is not None:
        for key, value in asdict(found.education).items():
            if value:
                filled[key] = value
                from_cv.add(key)
    profile = db.scalar(select(ApplicantProfile).where(ApplicantProfile.applicant_id == applicant.id))
    if "degree_title" not in filled and profile is not None and profile.degrees:
        filled["degree_title"] = profile.degrees[0]
    preference = db.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant.id))
    if preference is not None and preference.salary_amount and preference.salary_period in (None, "", "year"):
        filled["salary_expectation"] = format(preference.salary_amount.normalize(), "f")
    return replace(saved, extra={**filled, **saved.extra}), from_cv


def _education_body(request: Request, answers: AnswersInput, from_cv: set[str]) -> str:
    return f"""
<form method="post" action="/onboarding/education">
  {csrf_field(request)}
  <section class="card">
    <h1>Education and work</h1>
    <p class="muted">Graduate and junior application forms ask these almost every time. Every one is optional:
    anything you leave blank is left for you to answer on the employer's form.</p>
    {extra_fields(answers, "education", from_cv=from_cv)}
  </section>
  <section class="card">
    <h2>Work experience and pay</h2>
    {extra_fields(answers, "work", from_cv=from_cv)}
  </section>
  <div class="wizard-foot">{_back(OnboardingStep.education)}<button type="submit" class="primary">Continue</button></div>
</form>
"""


@router.get("/onboarding/education", response_model=None)
def education_step(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.education):
        return redirect
    answers, from_cv = _prefilled_education(db, applicant, input_from_row(load_answers(db, applicant.id)))
    notice = "We filled in what your CV shows. Check each answer marked 'from your CV'." if from_cv else None
    return _wizard_page(
        request, applicant, OnboardingStep.education, _education_body(request, answers, from_cv), notice=notice
    )


@router.post("/onboarding/education", response_model=None, dependencies=_CSRF)
async def education_save(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.education):
        return redirect
    answers = answers_from_form(await request.form(), input_from_row(load_answers(db, applicant.id)), (), _EDUCATION_EXTRAS)
    try:
        save_answers(db, applicant.id, answers)
    except AnswersRefused as exc:
        db.rollback()
        return _wizard_page(
            request, applicant, OnboardingStep.education, _education_body(request, answers, set()), error=exc.message, status_code=400
        )
    next_url = _advance(applicant, OnboardingStep.education)
    db.commit()
    return RedirectResponse(next_url, status_code=_SEE_OTHER)


# Step 6: availability and the questions most forms ask.


def _availability_body(request: Request, answers: AnswersInput) -> str:
    return f"""
<form method="post" action="/onboarding/availability">
  {csrf_field(request)}
  <section class="card">
    <h1>Availability</h1>
    <p class="muted">Application forms usually ask when you could start and how you want to work.</p>
    {availability_fields(answers, required=True)}
  </section>
  <section class="card">
    <h2>Questions most forms ask</h2>
    <p class="hint">Optional. A blank one is left for you on the employer's form.</p>
    {extra_fields(answers, "screening")}
  </section>
  <div class="wizard-foot">{_back(OnboardingStep.availability)}<button type="submit" class="primary">Continue</button></div>
</form>
"""


@router.get("/onboarding/availability", response_model=None)
def availability_step(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.availability):
        return redirect
    answers = input_from_row(load_answers(db, applicant.id))
    return _wizard_page(request, applicant, OnboardingStep.availability, _availability_body(request, answers))


@router.post("/onboarding/availability", response_model=None, dependencies=_CSRF)
async def availability_save(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.availability):
        return redirect
    answers = answers_from_form(
        await request.form(), input_from_row(load_answers(db, applicant.id)), _AVAILABILITY_FIELDS, _AVAILABILITY_EXTRAS
    )
    try:
        if not (answers.notice_period and answers.work_hours and answers.work_location) or answers.willing_to_relocate not in ("yes", "no"):
            raise AnswersRefused("Answer each question. Only the start date is optional.")
        save_answers(db, applicant.id, answers)
    except AnswersRefused as exc:
        db.rollback()
        return _wizard_page(
            request, applicant, OnboardingStep.availability, _availability_body(request, answers), error=exc.message, status_code=400
        )
    next_url = _advance(applicant, OnboardingStep.availability)
    db.commit()
    return RedirectResponse(next_url, status_code=_SEE_OTHER)


# Step 7: diversity questions, then finish.


def _diversity_body(request: Request, answers: AnswersInput) -> str:
    return f"""
<form method="post" action="/onboarding/diversity">
  {csrf_field(request)}
  <section class="card">
    <h1>Diversity questions</h1>
    <p class="muted">Optional. Many UK employers ask these to check their hiring is fair, and your answers
    don't affect your application. Each starts at prefer not to say, so you can leave any you'd rather not answer.
    We use them only to answer the same questions on employers' forms, and you can change them any time.</p>
    {diversity_fields(answers)}
    {extra_fields(answers, "diversity")}
  </section>
  <div class="wizard-foot">{_back(OnboardingStep.diversity)}<button type="submit" class="primary">Finish and find my jobs</button></div>
</form>
"""


@router.get("/onboarding/diversity", response_model=None)
def diversity_step(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.diversity):
        return redirect
    answers = input_from_row(load_answers(db, applicant.id))
    return _wizard_page(request, applicant, OnboardingStep.diversity, _diversity_body(request, answers))


@router.post("/onboarding/diversity", response_model=None, dependencies=_CSRF)
async def diversity_save(request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    if redirect := _guard(applicant, OnboardingStep.diversity):
        return redirect
    answers = answers_from_form(
        await request.form(), input_from_row(load_answers(db, applicant.id)), _DIVERSITY_FIELDS, _DIVERSITY_EXTRAS
    )
    try:
        save_answers(db, applicant.id, answers)
    except AnswersRefused as exc:
        db.rollback()
        return _wizard_page(
            request, applicant, OnboardingStep.diversity, _diversity_body(request, answers), error=exc.message, status_code=400
        )
    applicant.onboarding_step = None
    # Everything read from the CV has now been checked and saved where it belongs.
    if (cv := cv_for(db, applicant.id)) is not None:
        cv.suggestions = None
    db.commit()
    match_all(db, now=datetime.now(UTC), applicant_ids={applicant.id})
    db.commit()
    return RedirectResponse("/listings", status_code=_SEE_OTHER)
