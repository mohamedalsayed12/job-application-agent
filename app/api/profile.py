"""The profile form and CV upload. Every route acts only on the logged-in person (T30, T31)."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from itertools import zip_longest

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.accounts import throttle
from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import FormRefused, csrf_field, require_csrf
from app.api.interview_intel import interview_card
from app.api.jobs import get_gateway
from app.applicants.cv import (
    MAX_CV_BYTES,
    MAX_CVS,
    MAX_UPLOAD_BODY_BYTES,
    MEDIA_TYPES,
    CvRefused,
    base_cv,
    cvs_of,
    delete_cv,
    owned_cv,
    replace_cv,
    save_cv_choices,
    store_cv,
    suggest_label,
)
from app.applicants.cv_read import CvRead, CvSuggestions, ReadOutcome, read_cv
from app.applicants.levels import LEVELS, chosen_or_inferred, in_order
from app.applicants.profile import (
    MAX_ITEM_LENGTH,
    MAX_DEGREES,
    MAX_LOCATIONS,
    MAX_ROLES,
    MAX_SKILLS,
    CertificationInput,
    load_profile,
    parse_certification,
    parse_profile_input,
    save_profile,
)
from app.applicants.search_ideas import ProfileAsk, SearchIdeas, suggest_search
from app.applicants.service import PreferenceValidationError
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.matching.match import match_all
from app.models import Applicant, CvFile, CvFileType
from app.skills.taxonomy import suggest_skills

router = APIRouter(prefix="/profile", tags=["profile"])

_SEE_OTHER = status.HTTP_303_SEE_OTHER
_SPARE_CERTIFICATION_ROWS = 3
_SALARY_PERIODS = ("year", "month", "day", "hour")
_CSRF = [Depends(require_csrf)]
# Without scripts the form still works: people type their skills into the box.
_SKILL_SEARCH_SCRIPT = """<script>
const search = document.getElementById("skill-search");
const list = document.getElementById("skill-suggestions");
const skills = document.getElementById("skills");
search.addEventListener("input", async () => {
  const response = await fetch("/profile/skills/suggest?q=" + encodeURIComponent(search.value));
  if (!response.ok) return;
  list.replaceChildren(...(await response.json()).map((name) => {
    const option = document.createElement("option");
    option.value = name;
    return option;
  }));
});
document.getElementById("skill-add").addEventListener("click", () => {
  const name = search.value.trim();
  if (!name) return;
  skills.value = skills.value.trimEnd() + (skills.value.trim() ? "\\n" : "") + name;
  search.value = "";
  search.focus();
});
</script>"""


class ProfileFields:
    """What the form shows: the saved profile, or what was just typed when it needs correcting."""

    def __init__(
        self,
        target_roles: str = Form(""),
        locations: str = Form(""),
        salary_amount: str = Form(""),
        salary_currency: str = Form("GBP"),
        salary_period: str = Form("year"),
        experience_levels: list[str] = Form(default=[]),
        travel_distance: str = Form(""),
        skills: str = Form(""),
        cert_name: list[str] = Form(default=[]),
        cert_issuer: list[str] = Form(default=[]),
        cert_kind: list[str] = Form(default=[]),
        cert_expiry: list[str] = Form(default=[]),
        degrees: str = Form(""),
        cv_id: list[str] = Form(default=[]),
        cv_label: list[str] = Form(default=[]),
        cv_primary: str = Form(""),
    ) -> None:
        self.target_roles = target_roles
        self.locations = locations
        self.salary_amount = salary_amount
        self.salary_currency = salary_currency
        self.salary_period = salary_period
        self.experience_levels = experience_levels
        self.travel_distance = travel_distance
        self.skills = skills
        self.degrees = degrees
        self.certifications = [
            tuple(row) for row in zip_longest(cert_name, cert_issuer, cert_kind, cert_expiry, fillvalue="")
        ]
        self.cv_id = cv_id
        self.cv_label = cv_label
        self.cv_primary = cv_primary


def _saved_fields(db: Session, applicant: Applicant) -> ProfileFields:
    saved = load_profile(db, applicant.id)
    preference, profile = saved.preference, saved.profile
    certifications = saved.certifications
    return ProfileFields(
        target_roles="\n".join(preference.target_roles) if preference else "",
        locations="\n".join(preference.locations) if preference else "",
        salary_amount=str(preference.salary_amount) if preference and preference.salary_amount is not None else "",
        salary_currency=(preference.salary_currency if preference else None) or "GBP",
        salary_period=(preference.salary_period if preference else None) or "year",
        experience_levels=list(profile.experience_levels) if profile else [],
        travel_distance=str(profile.travel_distance_miles or "") if profile else "",
        skills="\n".join(saved.skills),
        cert_name=[item.name for item in certifications],
        cert_issuer=[item.issuer or "" for item in certifications],
        cert_kind=["licence" if item.is_licence else "certification" for item in certifications],
        cert_expiry=[str(item.expires_on or "") for item in certifications],
        degrees="\n".join(profile.degrees) if profile else "",
    )


def _options(pairs: list[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == selected else ""}>{html.escape(label)}</option>'
        for value, label in pairs
    )


def _certification_row(row: tuple[str, str, str, str]) -> str:
    name, issuer, kind, expiry = row
    kinds = _options([("certification", "Certification"), ("licence", "Licence")], kind)
    return (
        "<tr>"
        f'<td><input name="cert_name" aria-label="Name" value="{html.escape(name)}"/></td>'
        f'<td><input name="cert_issuer" aria-label="Issuer" value="{html.escape(issuer)}"/></td>'
        f'<td><select name="cert_kind" aria-label="Type">{kinds}</select></td>'
        f'<td><input name="cert_expiry" aria-label="Expiry date" type="date" value="{html.escape(expiry)}"/></td>'
        "</tr>"
    )


def _size_text(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.1f} MB" if size_bytes >= 1024 * 1024 else f"{max(size_bytes // 1024, 1)} KB"


def _cv_row(request: Request, cv: CvFile, *, label: str | None = None, checked: bool | None = None) -> str:
    shown = cv.label if label is None else label
    mark = " checked" if (cv.is_primary if checked is None else checked) else ""
    accept = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return f"""
<div class="cv-current">
  <span>
    <input form="profile-form" type="hidden" name="cv_id" value="{cv.id}"/>
    <input form="profile-form" name="cv_label" aria-label="Job this CV is for" placeholder="Job this CV is for"
      value="{html.escape(shown)}"/>
    <label><input form="profile-form" type="radio" name="cv_primary" value="{cv.id}"{mark}/> Primary</label>
    <a href="/profile/cv/{cv.id}">cv.{html.escape(cv.file_type)}</a>, {_size_text(cv.size_bytes)},
    uploaded {cv.uploaded_at:%d %B %Y}.
  </span>
  <span class="cv-actions">
    <form method="post" action="/profile/cv/{cv.id}/fill">{csrf_field(request)}<button type="submit" class="primary">Fill my profile from this CV</button></form>
    <form method="post" action="/profile/cv/{cv.id}/delete">{csrf_field(request)}<button type="submit" class="danger">Delete</button></form>
  </span>
</div>
<form method="post" action="/profile/cv/{cv.id}/replace" enctype="multipart/form-data" class="cv-replace">
  {csrf_field(request)}
  <input name="cv" type="file" required aria-label="New version of this CV" accept="{accept}"/>
  <button type="submit">Replace this CV</button>
</form>
"""


def _cv_section(request: Request, cvs: list[CvFile], error: str | None, fields: ProfileFields | None = None) -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    # A failed save still has the labels just typed. The database copy is the one that was rolled back.
    # A page built from the database leaves these as form markers, not a submitted list.
    submitted = (
        dict(zip(fields.cv_id, fields.cv_label, strict=False))
        if fields is not None and isinstance(fields.cv_id, list) and fields.cv_id
        else {}
    )
    chosen = fields.cv_primary if submitted and fields is not None and isinstance(fields.cv_primary, str) else None
    listed = "".join(
        _cv_row(
            request,
            cv,
            label=submitted[str(cv.id)] if str(cv.id) in submitted else None,
            checked=str(cv.id) == chosen if submitted else None,
        )
        for cv in cvs
    )
    hint = (
        f"You can keep up to {MAX_CVS}. Delete one before uploading another."
        if len(cvs) >= MAX_CVS
        else "PDF or Word (.docx), up to "
        f"{MAX_CV_BYTES // (1024 * 1024)} MB. You can keep up to {MAX_CVS}, each aimed at a different job."
    )
    return f"""
<section class="card">
<h2>4. Your CVs</h2>
{message}
{listed}
<p class="hint">The label says which job a CV is for. The primary one is used when none of the labels fit.
Press Save and find my matches to keep a label or a new primary. Reading a CV adds to your target roles and skills
and fills only the empty fields. Nothing there is saved until you press Save.</p>
<form method="post" action="/profile/cv" enctype="multipart/form-data" class="dropzone">
  {csrf_field(request)}
  <p><b>{"Upload another CV" if cvs else "Upload your CV"}</b></p>
  <p class="hint">{hint} When you upload it, AI reads it to fill in the form above for you to check.</p>
  <label>Job this CV is for <span class="hint">optional</span>
    <input name="label" placeholder="Data Analyst"/>
  </label>
  <input name="cv" type="file" required aria-label="CV file"
    accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"/>
  <p><button type="submit">Upload</button></p>
</form>
</section>
"""


_PROFILE_STYLE = """<style>
.card h2 { font-size: 1rem; }
.dropzone { border: 1.5px dashed #c7ccd4; border-radius: 8px; padding: 1rem; text-align: center; }
.dropzone input { max-width: 22rem; margin: 0.5rem auto; }
.cv-current { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap;
  font-size: 0.9rem; margin-bottom: 0.75rem; }
.cv-current form { margin: 0; }
.cv-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.notice { background: var(--primary-soft); color: var(--text); border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 1rem; }
.skill-find { display: flex; gap: 0.5rem; align-items: end; }
.skill-find label { flex: 1; }
.checks { border: 0; padding: 0; margin: 0.75rem 0 0; }
.checks legend { font-size: 0.85rem; font-weight: 600; padding: 0; }
.check { display: flex; align-items: center; gap: 0.5rem; font-weight: 400; margin-top: 0.35rem; }
.check input { display: inline; width: auto; margin: 0; }
.certs td { padding: 0.3rem; }
.certs input, .certs select { margin: 0; }
.form-foot { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; margin-top: 1.25rem; }
.cv-current input[name="cv_label"] { width: 14rem; margin: 0; }
.cv-replace { margin: 0 0 0.75rem; }
</style>"""


def _profile_page(
    request: Request,
    db: Session,
    applicant: Applicant,
    fields: ProfileFields,
    *,
    error: str | None = None,
    cv_error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    if notice:
        message += f'<div class="notice">{html.escape(notice)}</div>'
    levels = "".join(
        f'<label class="check"><input type="checkbox" name="experience_levels" value="{level.value}"'
        f'{" checked" if level.value in fields.experience_levels else ""}/> {html.escape(facts.label)}</label>'
        for level, facts in LEVELS.items()
    )
    periods = _options([(period, f"a {period}") for period in _SALARY_PERIODS], fields.salary_period)
    rows = [*fields.certifications, *[("", "", "certification", "")] * _SPARE_CERTIFICATION_ROWS]
    return page(
        "Your profile",
        f"""{_PROFILE_STYLE}
<div class="page-head"><div>
  <h1>Your profile</h1>
  <p class="muted">Tell us what you're looking for. We use this only to find and match jobs, and you can delete it at any time.
  For employer forms, set <a href="/profile/apply-answers">apply answers</a> separately.</p>
</div></div>
{message}
<form method="post" action="/profile" id="profile-form">
  {csrf_field(request)}
  <section class="card">
    <h2>1. Account</h2>
    <label>Email <input value="{html.escape(applicant.email)}" disabled/></label>
    <p class="hint">Change your password or delete your account on <a href="/account">your account page</a>.</p>
  </section>

  <section class="card">
    <h2>2. What you're looking for</h2>
    <label>Target roles <span class="hint">one per line</span>
      <textarea name="target_roles" rows="3" required placeholder="Junior Software Engineer">{html.escape(fields.target_roles)}</textarea>
    </label>
    <label>Locations <span class="hint">one per line</span>
      <textarea name="locations" rows="2" required placeholder="Manchester">{html.escape(fields.locations)}</textarea>
    </label>
    <p class="hint">Not sure what to search for? AI can suggest job titles that fit your skills and degrees, and towns
    near your locations, for you to check before anything is searched.
    <button type="submit" formaction="/profile/ideas" formnovalidate>Suggest roles and places</button></p>
    <fieldset class="checks">
      <legend>Experience levels <span class="hint">tick every level you want, at least one</span></legend>
      {levels}
    </fieldset>
    <p class="hint">We never show apprenticeships.</p>
    <label>How far you'll travel <span class="hint">miles, optional</span>
      <input name="travel_distance" inputmode="numeric" value="{html.escape(fields.travel_distance)}"/>
    </label>
    <div class="row">
      <label>Minimum salary <span class="hint">optional</span>
        <input name="salary_amount" inputmode="decimal" value="{html.escape(fields.salary_amount)}" placeholder="30000"/>
      </label>
      <label>Currency <input name="salary_currency" value="{html.escape(fields.salary_currency)}"/></label>
      <label>Per <select name="salary_period">{periods}</select></label>
    </div>
    <label>Degrees <span class="hint">one per line, including one you're studying for, optional</span>
      <textarea name="degrees" rows="2" placeholder="BSc Business Management">{html.escape(fields.degrees)}</textarea>
    </label>
    <p class="hint">Used when scoring how well a job fits you.</p>
  </section>

  <section class="card">
    <h2>3. Skills and certifications</h2>
    <div class="skill-find">
      <label>Find a skill
        <input id="skill-search" list="skill-suggestions" autocomplete="off" placeholder="Start typing, for example Python"/>
      </label>
      <button type="button" id="skill-add">Add</button>
    </div>
    <datalist id="skill-suggestions"></datalist>
    <label>Your skills <span class="hint">one per line</span>
      <textarea id="skills" name="skills" rows="5">{html.escape(fields.skills)}</textarea>
    </label>
    <p class="hint">This service uses the ESCO classification of the European Commission. We've added some technology
    skills ESCO doesn't list, so our skills list is an adapted version of it.</p>
    <label>Certifications and licences</label>
    <div class="table-wrap certs"><table>
      <thead><tr><th>Name</th><th>Issuer</th><th>Type</th><th>Expiry date</th></tr></thead>
      <tbody>{"".join(_certification_row(row) for row in rows)}</tbody>
    </table></div>
  </section>
</form>
{_cv_section(request, cvs_of(db, applicant.id), cv_error, fields)}
{interview_card(request, [line.strip() for line in fields.target_roles.splitlines() if line.strip()])}
<div class="form-foot">
  <a class="button danger" href="/account#delete">Delete my account and data</a>
  <button type="submit" class="primary" form="profile-form">Save and find my matches</button>
</div>
{_SKILL_SEARCH_SCRIPT}
""",
        status_code=status_code,
        header=applicant_bar(request, "profile"),
        narrow=True,
    )


@router.get("/skills/suggest")
def skill_suggestions(
    q: str = Query(default="", max_length=MAX_ITEM_LENGTH),
    _applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> list[str]:
    return suggest_skills(db, q)


@router.get("", response_class=HTMLResponse)
def profile_form(
    request: Request, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)
) -> HTMLResponse:
    return _profile_page(request, db, applicant, _saved_fields(db, applicant))


@router.post("", response_model=None, dependencies=_CSRF)
def profile_save(
    request: Request,
    fields: ProfileFields = Depends(),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        certifications: list[CertificationInput] = [
            item for row in fields.certifications if (item := parse_certification(*row))
        ]
        profile = parse_profile_input(
            target_roles=fields.target_roles.splitlines(),
            locations=fields.locations.splitlines(),
            salary_amount=fields.salary_amount,
            salary_currency=fields.salary_currency,
            salary_period=fields.salary_period,
            experience_levels=fields.experience_levels,
            travel_distance=fields.travel_distance,
            skills=fields.skills.splitlines(),
            certifications=certifications,
            degrees=fields.degrees.splitlines(),
        )
        save_profile(db, applicant.id, profile)
        _save_cv_choices(db, applicant.id, fields)
        db.commit()
        # A new profile sees its matches now, not after the next collection.
        match_all(db, now=datetime.now(UTC), applicant_ids={applicant.id})
        db.commit()
    except (PreferenceValidationError, CvRefused) as exc:
        db.rollback()
        return _profile_page(request, db, applicant, fields, error=str(exc), status_code=400)
    return RedirectResponse("/listings", status_code=_SEE_OTHER)


def limit_cv_upload(request: Request, applicant: Applicant = Depends(require_login)) -> None:
    """Runs before the body is read, so an oversized upload never reaches the app's memory or disk."""
    length = request.headers.get("content-length")
    if length is None or not length.isdecimal():
        raise FormRefused(status.HTTP_411_LENGTH_REQUIRED, "Your browser didn't say how big the file is. Try again.")
    if int(length) > MAX_UPLOAD_BODY_BYTES:
        raise FormRefused(status.HTTP_413_CONTENT_TOO_LARGE, "That file is over the 5 MB limit.")
    if not throttle.take_cv_upload_attempt(str(applicant.id)):
        raise FormRefused(status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads. Wait a while and try again.")


async def read_cv_upload(request: Request) -> tuple[bytes | None, str]:
    form = await request.form()
    try:
        upload = form.get("cv")
        # One byte over the limit is enough for check_cv to refuse the file.
        content = await upload.read(MAX_CV_BYTES + 1) if isinstance(upload, UploadFile) else None
        raw_label = form.get("label")
        return content, raw_label if isinstance(raw_label, str) else ""
    finally:
        await form.close()


_READ_PROBLEMS = {
    ReadOutcome.unreadable: "We couldn't find text in your CV. A scanned PDF has none. Fill in the form yourself.",
    ReadOutcome.budget: "This month's AI budget is used up, so your CV wasn't read. Fill in the form yourself.",
    ReadOutcome.provider_failed: "Reading your CV didn't work this time. Try again later, or fill in the form yourself.",
    ReadOutcome.not_configured: "Reading CVs isn't set up on this server. Fill in the form yourself.",
}


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _save_cv_choices(db: Session, applicant_id: uuid.UUID, fields: ProfileFields) -> None:
    try:
        labels = {
            uuid.UUID(str(raw_id)): label
            for raw_id, label in zip(fields.cv_id, fields.cv_label, strict=True)
        }
        primary = uuid.UUID(str(fields.cv_primary)) if fields.cv_primary else None
    except ValueError as exc:
        raise CvRefused("One of these CVs isn't valid. Reload the page and try again.") from exc
    if labels or primary is not None:
        save_cv_choices(db, applicant_id, labels, primary)


def fill_from_cv(fields: ProfileFields, found: CvSuggestions) -> tuple[ProfileFields, list[str]]:
    """The form with the CV's suggestions added. What the person already has always stays."""
    filled = []
    roles = _lines(fields.target_roles)
    known_roles = {role.casefold() for role in roles}
    room = max(MAX_ROLES - len(roles), 0)
    new_roles = [role for role in found.target_roles if role.casefold() not in known_roles][:room]
    if new_roles:
        filled.append("target roles" if not roles else f"{len(new_roles)} target role{'s' if len(new_roles) != 1 else ''}")
    roles_text = "\n".join([*roles, *new_roles])
    locations, levels = fields.locations, fields.experience_levels
    if not locations.strip() and found.locations:
        locations = "\n".join(found.locations)
        filled.append("locations")
    if not levels and found.experience_levels:
        levels = found.experience_levels
        filled.append("experience levels")
    skills = _lines(fields.skills)
    known = {skill.casefold() for skill in skills}
    new_skills = [skill for skill in found.skills if skill.casefold() not in known]
    if new_skills:
        filled.append(f"{len(new_skills)} skill{'s' if len(new_skills) != 1 else ''}")
    rows = [row for row in fields.certifications if any(part.strip() for part in row[:2])]
    held = {row[0].strip().casefold() for row in rows}
    new_rows = [
        (item.name, item.issuer, "licence" if item.is_licence else "certification", item.expires_on)
        for item in found.certifications
        if item.name.casefold() not in held
    ]
    if new_rows:
        filled.append(f"{len(new_rows)} certification{'s' if len(new_rows) != 1 else ''} or licence{'s' if len(new_rows) != 1 else ''}")
    rows += new_rows
    degrees = _lines(fields.degrees)
    studied = {degree.casefold() for degree in degrees}
    new_degrees = [degree for degree in found.degrees if degree.casefold() not in studied]
    if new_degrees:
        filled.append(f"{len(new_degrees)} degree{'s' if len(new_degrees) != 1 else ''}")
    merged = ProfileFields(
        target_roles=roles_text,
        locations=locations,
        salary_amount=fields.salary_amount,
        salary_currency=fields.salary_currency,
        salary_period=fields.salary_period,
        experience_levels=levels,
        travel_distance=fields.travel_distance,
        skills="\n".join([*skills, *new_skills]),
        cert_name=[row[0] for row in rows],
        cert_issuer=[row[1] for row in rows],
        cert_kind=[row[2] for row in rows],
        cert_expiry=[row[3] for row in rows],
        degrees="\n".join([*degrees, *new_degrees]),
    )
    return merged, filled


def _after_read(request: Request, db: Session, applicant: Applicant, read: CvRead) -> HTMLResponse:
    fields = _saved_fields(db, applicant)
    if read.suggestions is None:
        return _profile_page(request, db, applicant, fields, notice=_READ_PROBLEMS[read.outcome])
    fields, filled = fill_from_cv(fields, read.suggestions)
    if not filled:
        notice = "We read your CV but found nothing new to add to your profile."
    else:
        notice = (
            f"We read your CV and filled in {', '.join(filled)}. Check everything, change anything that's wrong, "
            "then press Save and find my matches. Nothing is saved until you do."
        )
    return _profile_page(request, db, applicant, fields, notice=notice)


_IDEA_PROBLEMS = {
    ReadOutcome.budget: "This month's AI budget is used up, so we couldn't suggest roles and places.",
    ReadOutcome.provider_failed: "Suggesting roles and places didn't work this time. Try again later.",
    ReadOutcome.not_configured: "Suggesting roles and places isn't set up on this server.",
}


def _ask(fields: ProfileFields) -> ProfileAsk:
    """Unsaved form text, so it is held to the profile limits before it goes to the AI at a fixed price."""

    def capped(text: str, limit: int) -> tuple[str, ...]:
        return tuple(line for line in _lines(text) if len(line) <= MAX_ITEM_LENGTH)[:limit]

    travel = fields.travel_distance.strip()
    roles = capped(fields.target_roles, MAX_ROLES)
    ticked = frozenset(level for level in LEVELS if level.value in fields.experience_levels)
    return ProfileAsk(
        skills=capped(fields.skills, MAX_SKILLS),
        degrees=capped(fields.degrees, MAX_DEGREES),
        experience_levels=tuple(level.value for level in in_order(chosen_or_inferred(ticked, roles))),
        target_roles=roles,
        locations=capped(fields.locations, MAX_LOCATIONS),
        travel_miles=int(travel) if travel.isdecimal() and len(travel) <= 3 else None,
    )


def add_ideas(fields: ProfileFields, ideas: SearchIdeas, settings: Settings) -> tuple[ProfileFields, str]:
    """The form with the suggested roles and places added, up to the profile limits, and what to tell the person."""
    roles, places = _lines(fields.target_roles), _lines(fields.locations)
    new_roles = list(ideas.roles[: max(MAX_ROLES - len(roles), 0)])
    new_places = list(ideas.places[: max(MAX_LOCATIONS - len(places), 0)])
    if not new_roles and not new_places:
        full = len(ideas.roles) + len(ideas.places)
        return fields, (
            f"Your profile already has the most roles and places it can hold ({MAX_ROLES} each)."
            if full
            else "We found no new roles or places to suggest."
        )
    roles += [idea.title for idea in new_roles]
    places += new_places
    merged = ProfileFields(
        target_roles="\n".join(roles),
        locations="\n".join(places),
        salary_amount=fields.salary_amount,
        salary_currency=fields.salary_currency,
        salary_period=fields.salary_period,
        experience_levels=fields.experience_levels,
        travel_distance=fields.travel_distance,
        skills=fields.skills,
        cert_name=[row[0] for row in fields.certifications],
        cert_issuer=[row[1] for row in fields.certifications],
        cert_kind=[row[2] for row in fields.certifications],
        cert_expiry=[row[3] for row in fields.certifications],
        degrees=fields.degrees,
    )
    said = []
    if new_roles:
        said.append("Roles: " + "; ".join(f"{idea.title} (from your {', '.join(idea.because)})" for idea in new_roles) + ".")
    if new_places:
        said.append(f"Places near you: {', '.join(new_places)}.")
    searches = len(roles) * max(len(places), 1)
    cost = searches * (settings.linkedin_max_results * settings.linkedin_unit_price + settings.linkedin_run_price)
    said.append(
        "They're in the form now. Remove any you don't want, then press Save and find my matches. "
        f"As it stands, your profile runs {searches} LinkedIn search{'es' if searches != 1 else ''} a day, "
        f"costing up to £{cost:.2f}."
    )
    return merged, " ".join(said)


@router.post("/ideas", response_model=None, dependencies=_CSRF)
async def search_ideas(
    request: Request,
    fields: ProfileFields = Depends(),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    """Works on what the form holds now, saved or not, and saves nothing."""
    ask = _ask(fields)
    if not ask.skills and not ask.degrees:
        return _profile_page(request, db, applicant, fields, notice="Add your skills or a degree first, then ask for suggestions.")
    if not throttle.take_cv_upload_attempt(str(applicant.id)):
        raise FormRefused(status.HTTP_429_TOO_MANY_REQUESTS, "Too many tries. Wait a while and try again.")
    settings = get_settings()
    found = await suggest_search(db, applicant.id, ask, gateway=gateway, settings=settings)
    if isinstance(found, ReadOutcome):
        return _profile_page(request, db, applicant, fields, notice=_IDEA_PROBLEMS[found])
    fields, notice = add_ideas(fields, found, settings)
    return _profile_page(request, db, applicant, fields, notice=notice)


def _cv_response(cv: CvFile) -> Response:
    return Response(
        cv.content,
        media_type=MEDIA_TYPES[CvFileType(cv.file_type)],
        headers={
            "Content-Disposition": f'attachment; filename="cv.{cv.file_type}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _missing_cv() -> HTMLResponse:
    return page("No CV", '<h1>No CV</h1><p><a href="/profile">Back to your profile</a></p>', status_code=404)


async def _show_read(
    request: Request, db: Session, applicant: Applicant, cv: CvFile, content: bytes, gateway: FetchGateway
) -> Response:
    read = await read_cv(
        db, applicant.id, content, CvFileType(cv.file_type), gateway=gateway, settings=get_settings()
    )
    if read.suggestions is not None and read.suggestions.aimed_at and not cv.label:
        suggest_label(cv, read.suggestions.aimed_at)
        db.commit()
    if read.outcome == ReadOutcome.not_configured:
        return RedirectResponse("/profile", status_code=_SEE_OTHER)
    return _after_read(request, db, applicant, read)


@router.post(
    "/cv",
    response_model=None,
    dependencies=[Depends(require_login), Depends(limit_cv_upload), Depends(require_csrf)],
)
async def cv_upload(
    request: Request,
    upload: tuple[bytes | None, str] = Depends(read_cv_upload),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    content, label = upload
    try:
        cv = store_cv(db, applicant.id, content, label=label)
        db.commit()
    except CvRefused as exc:
        db.rollback()
        return _profile_page(request, db, applicant, _saved_fields(db, applicant), cv_error=str(exc), status_code=400)
    return await _show_read(request, db, applicant, cv, content or b"", gateway)


@router.post(
    "/cv/{cv_id}/replace",
    response_model=None,
    dependencies=[Depends(require_login), Depends(limit_cv_upload), Depends(require_csrf)],
)
async def cv_replace(
    cv_id: uuid.UUID,
    request: Request,
    upload: tuple[bytes | None, str] = Depends(read_cv_upload),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    content, label = upload
    try:
        cv = replace_cv(db, applicant.id, cv_id, content, label=label)
        db.commit()
    except CvRefused as exc:
        db.rollback()
        return _profile_page(request, db, applicant, _saved_fields(db, applicant), cv_error=str(exc), status_code=400)
    return await _show_read(request, db, applicant, cv, content or b"", gateway)


async def _fill_one(
    request: Request, db: Session, applicant: Applicant, cv: CvFile | None, gateway: FetchGateway
) -> Response:
    if cv is None:
        return RedirectResponse("/profile", status_code=_SEE_OTHER)
    if not throttle.take_cv_upload_attempt(str(applicant.id)):
        raise FormRefused(status.HTTP_429_TOO_MANY_REQUESTS, "Too many tries. Wait a while and try again.")
    return await _show_read(request, db, applicant, cv, bytes(cv.content), gateway)


@router.post("/cv/fill", response_model=None, dependencies=_CSRF)
async def cv_fill(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    return await _fill_one(request, db, applicant, base_cv(db, applicant.id), gateway)


@router.post("/cv/{cv_id}/fill", response_model=None, dependencies=_CSRF)
async def cv_fill_one(
    cv_id: uuid.UUID,
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> Response:
    return await _fill_one(request, db, applicant, owned_cv(db, applicant.id, cv_id), gateway)


@router.get("/cv")
def cv_download(applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    """The primary CV. A specific one is /profile/cv/{id}, and another person's id is a 404."""
    cv = base_cv(db, applicant.id)
    if cv is None:
        return _missing_cv()
    return _cv_response(cv)


@router.get("/cv/{cv_id}")
def cv_download_one(
    cv_id: uuid.UUID, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    cv = owned_cv(db, applicant.id, cv_id)
    if cv is None:
        return _missing_cv()
    return _cv_response(cv)


@router.post("/cv/{cv_id}/delete", dependencies=_CSRF)
def cv_delete(
    cv_id: uuid.UUID, applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)
) -> RedirectResponse:
    delete_cv(db, applicant.id, cv_id)
    db.commit()
    return RedirectResponse("/profile", status_code=_SEE_OTHER)
