"""The profile form and CV upload. Every route acts only on the logged-in person (T30, T31)."""

from __future__ import annotations

import html
from itertools import zip_longest

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.accounts import throttle
from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.forms import FormRefused, csrf_field, require_csrf
from app.applicants.cv import (
    MAX_CV_BYTES,
    MAX_UPLOAD_BODY_BYTES,
    MEDIA_TYPES,
    CvRefused,
    cv_for,
    delete_cv,
    store_cv,
)
from app.applicants.profile import (
    EXPERIENCE_LABELS,
    MAX_ITEM_LENGTH,
    CertificationInput,
    load_profile,
    parse_certification,
    parse_profile_input,
    save_profile,
)
from app.applicants.service import PreferenceValidationError
from app.core.db import get_db
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
        experience_level: str = Form(""),
        travel_distance: str = Form(""),
        skills: str = Form(""),
        cert_name: list[str] = Form(default=[]),
        cert_issuer: list[str] = Form(default=[]),
        cert_kind: list[str] = Form(default=[]),
        cert_expiry: list[str] = Form(default=[]),
    ) -> None:
        self.target_roles = target_roles
        self.locations = locations
        self.salary_amount = salary_amount
        self.salary_currency = salary_currency
        self.salary_period = salary_period
        self.experience_level = experience_level
        self.travel_distance = travel_distance
        self.skills = skills
        self.certifications = [
            tuple(row) for row in zip_longest(cert_name, cert_issuer, cert_kind, cert_expiry, fillvalue="")
        ]


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
        experience_level=profile.experience_level if profile else "",
        travel_distance=str(profile.travel_distance_miles or "") if profile else "",
        skills="\n".join(saved.skills),
        cert_name=[item.name for item in certifications],
        cert_issuer=[item.issuer or "" for item in certifications],
        cert_kind=["licence" if item.is_licence else "certification" for item in certifications],
        cert_expiry=[str(item.expires_on or "") for item in certifications],
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


def _cv_section(request: Request, cv: CvFile | None, error: str | None) -> str:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    current = (
        f"""
<p>Your CV: <a href="/profile/cv">cv.{html.escape(cv.file_type)}</a>, {_size_text(cv.size_bytes)},
uploaded {cv.uploaded_at:%d %B %Y}.</p>
<form method="post" action="/profile/cv/delete">{csrf_field(request)}<button type="submit">Delete CV</button></form>
"""
        if cv
        else "<p>You haven't uploaded a CV.</p>"
    )
    return f"""
<h2>CV</h2>
<p>PDF or Word (.docx), up to {MAX_CV_BYTES // (1024 * 1024)} MB. It's private to you, and nothing reads its contents.</p>
{message}
{current}
<form method="post" action="/profile/cv" enctype="multipart/form-data">
  {csrf_field(request)}
  <label>{"Replace your CV" if cv else "Upload your CV"}
    <input name="cv" type="file" required
      accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"/>
  </label>
  <p><button type="submit">Upload</button></p>
</form>
"""


def _profile_page(
    request: Request,
    db: Session,
    applicant: Applicant,
    fields: ProfileFields,
    *,
    error: str | None = None,
    cv_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    levels = _options(
        [("", "Choose one"), *((level.value, label) for level, label in EXPERIENCE_LABELS.items())],
        fields.experience_level,
    )
    periods = _options([(period, period) for period in _SALARY_PERIODS], fields.salary_period)
    rows = [*fields.certifications, *[("", "", "certification", "")] * _SPARE_CERTIFICATION_ROWS]
    return page(
        "Your profile",
        f"""
<h1>Your profile</h1>
<p><a href="/account">Your account</a></p>
{message}
<form method="post" action="/profile">
  {csrf_field(request)}
  <label>Target roles (one per line)
    <textarea name="target_roles" rows="3" required>{html.escape(fields.target_roles)}</textarea>
  </label>
  <label>Locations (one per line)
    <textarea name="locations" rows="3" required>{html.escape(fields.locations)}</textarea>
  </label>
  <label>Experience level
    <select name="experience_level" required>{levels}</select>
  </label>
  <label>How far you'll travel, in miles (optional)
    <input name="travel_distance" inputmode="numeric" value="{html.escape(fields.travel_distance)}"/>
  </label>
  <h2>Expected salary (optional)</h2>
  <label>Amount <input name="salary_amount" inputmode="decimal" value="{html.escape(fields.salary_amount)}"/></label>
  <label>Currency <input name="salary_currency" value="{html.escape(fields.salary_currency)}"/></label>
  <label>Period <select name="salary_period">{periods}</select></label>
  <h2>Skills</h2>
  <label>Find a skill
    <input id="skill-search" list="skill-suggestions" autocomplete="off"/>
  </label>
  <datalist id="skill-suggestions"></datalist>
  <p><button type="button" id="skill-add">Add to my skills</button></p>
  <label>Your skills (one per line)
    <textarea id="skills" name="skills" rows="5">{html.escape(fields.skills)}</textarea>
  </label>
  <p>This service uses the ESCO classification of the European Commission. We've added some technology
  skills ESCO doesn't list, so our skills list is an adapted version of it.</p>
  <h2>Certifications and licences</h2>
  <table>
    <thead><tr><th>Name</th><th>Issuer</th><th>Type</th><th>Expiry date</th></tr></thead>
    <tbody>{"".join(_certification_row(row) for row in rows)}</tbody>
  </table>
  <p><button type="submit">Save profile</button></p>
</form>
{_cv_section(request, cv_for(db, applicant.id), cv_error)}
{_SKILL_SEARCH_SCRIPT}
""",
        status_code=status_code,
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
            experience_level=fields.experience_level,
            travel_distance=fields.travel_distance,
            skills=fields.skills.splitlines(),
            certifications=certifications,
        )
        save_profile(db, applicant.id, profile)
        db.commit()
    except PreferenceValidationError as exc:
        db.rollback()
        return _profile_page(request, db, applicant, fields, error=str(exc), status_code=400)
    return RedirectResponse("/profile", status_code=_SEE_OTHER)


def limit_cv_upload(request: Request, applicant: Applicant = Depends(require_login)) -> None:
    """Runs before the body is read, so an oversized upload never reaches the app's memory or disk."""
    length = request.headers.get("content-length")
    if length is None or not length.isdecimal():
        raise FormRefused(status.HTTP_411_LENGTH_REQUIRED, "Your browser didn't say how big the file is. Try again.")
    if int(length) > MAX_UPLOAD_BODY_BYTES:
        raise FormRefused(status.HTTP_413_CONTENT_TOO_LARGE, "That file is over the 5 MB limit.")
    if not throttle.take_cv_upload_attempt(str(applicant.id)):
        raise FormRefused(status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads. Wait a while and try again.")


async def read_cv_upload(request: Request) -> bytes | None:
    form = await request.form()
    try:
        upload = form.get("cv")
        # One byte over the limit is enough for check_cv to refuse the file.
        return await upload.read(MAX_CV_BYTES + 1) if isinstance(upload, UploadFile) else None
    finally:
        await form.close()


@router.post(
    "/cv",
    response_model=None,
    dependencies=[Depends(require_login), Depends(limit_cv_upload), Depends(require_csrf)],
)
def cv_upload(
    request: Request,
    content: bytes | None = Depends(read_cv_upload),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        store_cv(db, applicant.id, content)
        db.commit()
    except CvRefused as exc:
        db.rollback()
        return _profile_page(request, db, applicant, _saved_fields(db, applicant), cv_error=str(exc), status_code=400)
    return RedirectResponse("/profile", status_code=_SEE_OTHER)


@router.get("/cv")
def cv_download(applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    """There is no file ID in the URL, so the only CV anyone can ask for is their own."""
    cv = cv_for(db, applicant.id)
    if cv is None:
        return page("No CV", '<h1>No CV</h1><p><a href="/profile">Back to your profile</a></p>', status_code=404)
    return Response(
        cv.content,
        media_type=MEDIA_TYPES[CvFileType(cv.file_type)],
        headers={
            "Content-Disposition": f'attachment; filename="cv.{cv.file_type}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/cv/delete", dependencies=_CSRF)
def cv_delete(applicant: Applicant = Depends(require_login), db: Session = Depends(get_db)) -> RedirectResponse:
    delete_cv(db, applicant.id)
    db.commit()
    return RedirectResponse("/profile", status_code=_SEE_OTHER)
