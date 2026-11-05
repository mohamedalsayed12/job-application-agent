"""Admin create/edit HTML for Milestone 1 applicants."""

from __future__ import annotations

import html
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin_key
from app.applicants.service import (
    ApplicantConflictError,
    ApplicantNotFoundError,
    PreferenceValidationError,
    create_applicant,
    get_applicant_with_preference,
    list_applicants,
    parse_preference_input,
    update_applicant,
)
from app.core.db import get_db

router = APIRouter(prefix="/admin/applicants", tags=["admin-applicants"])


class ApplicantForm:
    """Shared create/edit form fields."""

    def __init__(
        self,
        email: str = Form(...),
        display_name: str = Form(""),
        target_roles: str = Form(...),
        locations: str = Form(...),
        sector: str = Form("tech"),
        salary_amount: str = Form(""),
        salary_currency: str = Form(""),
        salary_period: str = Form("year"),
    ) -> None:
        self.email = email
        self.display_name = display_name
        self.target_roles = target_roles
        self.locations = locations
        self.sector = sector
        self.salary_amount = salary_amount
        self.salary_currency = salary_currency
        self.salary_period = salary_period


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem; }}
    label {{ display: block; margin-top: 0.75rem; }}
    input, textarea, select {{ width: 100%; box-sizing: border-box; }}
    .error {{ color: #a00; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border-bottom: 1px solid #ccc; text-align: left; padding: 0.35rem; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""
    return HTMLResponse(doc)


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _action(path: str, admin_key: str) -> str:
    return f"{path}?admin_key={quote(admin_key, safe='')}"


def _form(
    *,
    action: str,
    admin_key: str,
    email: str = "",
    display_name: str = "",
    target_roles: str = "",
    locations: str = "",
    salary_amount: str = "",
    salary_currency: str = "GBP",
    salary_period: str = "year",
    error: str | None = None,
    submit_label: str = "Save",
) -> str:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""
<h1>Applicant</h1>
{err}
<form method="post" action="{html.escape(action)}">
  <label>Email
    <input name="email" type="email" required value="{html.escape(email)}"/>
  </label>
  <label>Display name
    <input name="display_name" value="{html.escape(display_name)}"/>
  </label>
  <label>Target roles (one per line)
    <textarea name="target_roles" rows="3" required>{html.escape(target_roles)}</textarea>
  </label>
  <label>Locations (one per line)
    <textarea name="locations" rows="3" required>{html.escape(locations)}</textarea>
  </label>
  <label>Sector
    <input name="sector" value="tech" readonly/>
  </label>
  <p>Expected salary (optional matching preference — not used for search or budget)</p>
  <label>Amount
    <input name="salary_amount" inputmode="decimal" value="{html.escape(salary_amount)}"/>
  </label>
  <label>Currency
    <input name="salary_currency" value="{html.escape(salary_currency)}"/>
  </label>
  <label>Period
    <select name="salary_period">
      <option value="year" {"selected" if salary_period == "year" else ""}>year</option>
      <option value="month" {"selected" if salary_period == "month" else ""}>month</option>
      <option value="day" {"selected" if salary_period == "day" else ""}>day</option>
      <option value="hour" {"selected" if salary_period == "hour" else ""}>hour</option>
    </select>
  </label>
  <p style="margin-top:1rem"><button type="submit">{html.escape(submit_label)}</button></p>
</form>
<p><a href="{html.escape(_action('/admin/applicants', admin_key))}">Back to list</a></p>
"""


def _save(
    db: Session,
    form: ApplicantForm,
    *,
    admin_key: str,
    applicant_id: uuid.UUID | None,
) -> HTMLResponse:
    is_create = applicant_id is None
    title = "New applicant" if is_create else "Edit applicant"
    path = (
        "/admin/applicants/new"
        if is_create
        else f"/admin/applicants/{applicant_id}/edit"
    )
    action = _action(path, admin_key)
    submit_label = "Create" if is_create else "Update"
    form_kwargs = dict(
        action=action,
        admin_key=admin_key,
        email=form.email,
        display_name=form.display_name,
        target_roles=form.target_roles,
        locations=form.locations,
        salary_amount=form.salary_amount,
        salary_currency=form.salary_currency,
        salary_period=form.salary_period,
        submit_label=submit_label,
    )
    try:
        amount: Decimal | str | None = form.salary_amount
        if isinstance(amount, str) and not amount.strip():
            amount = None
        elif isinstance(amount, str):
            amount = Decimal(amount.strip())
        pref = parse_preference_input(
            target_roles=_split_lines(form.target_roles),
            locations=_split_lines(form.locations),
            sector=form.sector,
            salary_amount=amount,
            salary_currency=form.salary_currency or None,
            salary_period=form.salary_period or None,
        )
        if applicant_id is None:
            create_applicant(
                db,
                email=form.email,
                preference=pref,
                display_name=form.display_name or None,
            )
        else:
            update_applicant(
                db,
                applicant_id,
                preference=pref,
                display_name=form.display_name,
                email=form.email,
            )
        db.commit()
    except InvalidOperation:
        return _page(title, _form(**form_kwargs, error="salary amount must be a number"))
    except ApplicantNotFoundError:
        db.rollback()
        return _page("Not found", "<p class='error'>applicant not found</p>")
    except (PreferenceValidationError, ApplicantConflictError) as exc:
        db.rollback()
        return _page(title, _form(**form_kwargs, error=str(exc)))

    return RedirectResponse(
        url=_action("/admin/applicants", admin_key),
        status_code=303,
    )


@router.get("", response_class=HTMLResponse)
def applicants_index(
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = list_applicants(db)
    items = []
    for applicant, pref in rows:
        roles = ", ".join(pref.target_roles)
        locs = ", ".join(pref.locations)
        salary = (
            f"{pref.salary_amount} {pref.salary_currency}/{pref.salary_period}"
            if pref.salary_amount is not None
            else "(none)"
        )
        edit = _action(f"/admin/applicants/{applicant.id}/edit", admin_key)
        items.append(
            "<tr>"
            f"<td>{html.escape(applicant.email)}</td>"
            f"<td>{html.escape(roles)}</td>"
            f"<td>{html.escape(locs)}</td>"
            f"<td>{html.escape(salary)}</td>"
            f'<td><a href="{html.escape(edit)}">Edit</a></td>'
            "</tr>"
        )
    body = f"""
<h1>Applicants</h1>
<p><a href="{html.escape(_action('/admin/applicants/new', admin_key))}">Create applicant</a></p>
<table>
  <thead><tr><th>Email</th><th>Roles</th><th>Locations</th><th>Salary</th><th></th></tr></thead>
  <tbody>
    {"".join(items) if items else "<tr><td colspan='5'>No applicants yet.</td></tr>"}
  </tbody>
</table>
"""
    return _page("Applicants", body)


@router.get("/new", response_class=HTMLResponse)
def applicants_new_form(admin_key: str = Depends(require_admin_key)) -> HTMLResponse:
    return _page(
        "New applicant",
        _form(
            action=_action("/admin/applicants/new", admin_key),
            admin_key=admin_key,
            submit_label="Create",
        ),
    )


@router.post("/new", response_class=HTMLResponse)
def applicants_create(
    form: Annotated[ApplicantForm, Depends()],
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _save(db, form, admin_key=admin_key, applicant_id=None)


@router.get("/{applicant_id}/edit", response_class=HTMLResponse)
def applicants_edit_form(
    applicant_id: uuid.UUID,
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    row = get_applicant_with_preference(db, applicant_id)
    if row is None:
        return _page("Not found", "<p class='error'>applicant not found</p>")
    applicant, pref = row
    return _page(
        "Edit applicant",
        _form(
            action=_action(f"/admin/applicants/{applicant_id}/edit", admin_key),
            admin_key=admin_key,
            email=applicant.email,
            display_name=applicant.display_name or "",
            target_roles="\n".join(pref.target_roles),
            locations="\n".join(pref.locations),
            salary_amount="" if pref.salary_amount is None else str(pref.salary_amount),
            salary_currency=pref.salary_currency or "GBP",
            salary_period=pref.salary_period or "year",
            submit_label="Update",
        ),
    )


@router.post("/{applicant_id}/edit", response_class=HTMLResponse)
def applicants_update(
    applicant_id: uuid.UUID,
    form: Annotated[ApplicantForm, Depends()],
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _save(db, form, admin_key=admin_key, applicant_id=applicant_id)
