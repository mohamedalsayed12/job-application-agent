"""Admin create/edit HTML for Milestone 1 applicants."""

from __future__ import annotations

import html
import secrets
import uuid
from decimal import Decimal, InvalidOperation

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
from app.core.config import get_settings
from app.core.db import get_db

router = APIRouter(prefix="/admin/applicants", tags=["admin-applicants"])


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
    return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]


def _form_admin_ok(admin_key: str) -> bool:
    expected = get_settings().admin_key or ""
    return bool(expected) and secrets.compare_digest(admin_key, expected)


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
  <input type="hidden" name="admin_key" value="{html.escape(admin_key)}"/>
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
<p><a href="/admin/applicants?admin_key={html.escape(admin_key)}">Back to list</a></p>
"""


def _parse_amount(raw: str) -> Decimal | None:
    text = raw.strip()
    if not text:
        return None
    return Decimal(text)


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
        edit = f"/admin/applicants/{applicant.id}/edit?admin_key={html.escape(admin_key)}"
        items.append(
            "<tr>"
            f"<td>{html.escape(applicant.email)}</td>"
            f"<td>{html.escape(roles)}</td>"
            f"<td>{html.escape(locs)}</td>"
            f"<td>{html.escape(salary)}</td>"
            f'<td><a href="{edit}">Edit</a></td>'
            "</tr>"
        )
    body = f"""
<h1>Applicants</h1>
<p><a href="/admin/applicants/new?admin_key={html.escape(admin_key)}">Create applicant</a></p>
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
        _form(action="/admin/applicants/new", admin_key=admin_key, submit_label="Create"),
    )


@router.post("/new", response_class=HTMLResponse)
def applicants_create(
    email: str = Form(...),
    display_name: str = Form(""),
    target_roles: str = Form(...),
    locations: str = Form(...),
    sector: str = Form("tech"),
    salary_amount: str = Form(""),
    salary_currency: str = Form(""),
    salary_period: str = Form("year"),
    admin_key: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not _form_admin_ok(admin_key):
        return _page("Unauthorized", "<p class='error'>invalid or missing admin key</p>")

    form_kwargs = dict(
        action="/admin/applicants/new",
        admin_key=admin_key,
        email=email,
        display_name=display_name,
        target_roles=target_roles,
        locations=locations,
        salary_amount=salary_amount,
        salary_currency=salary_currency,
        salary_period=salary_period,
        submit_label="Create",
    )
    try:
        amount = _parse_amount(salary_amount)
    except InvalidOperation:
        return _page("New applicant", _form(**form_kwargs, error="salary amount must be a number"))

    try:
        pref = parse_preference_input(
            target_roles=_split_lines(target_roles),
            locations=_split_lines(locations),
            sector=sector,
            salary_amount=amount,
            salary_currency=(salary_currency or None) if amount is not None else None,
            salary_period=salary_period if amount is not None else None,
        )
        create_applicant(
            db, email=email, preference=pref, display_name=display_name or None
        )
        db.commit()
    except (PreferenceValidationError, ApplicantConflictError) as exc:
        db.rollback()
        return _page("New applicant", _form(**form_kwargs, error=str(exc)))

    return RedirectResponse(
        url=f"/admin/applicants?admin_key={admin_key}",
        status_code=303,
    )


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
            action=f"/admin/applicants/{applicant_id}/edit",
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
    email: str = Form(...),
    display_name: str = Form(""),
    target_roles: str = Form(...),
    locations: str = Form(...),
    sector: str = Form("tech"),
    salary_amount: str = Form(""),
    salary_currency: str = Form(""),
    salary_period: str = Form("year"),
    admin_key: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not _form_admin_ok(admin_key):
        return _page("Unauthorized", "<p class='error'>invalid or missing admin key</p>")

    form_kwargs = dict(
        action=f"/admin/applicants/{applicant_id}/edit",
        admin_key=admin_key,
        email=email,
        display_name=display_name,
        target_roles=target_roles,
        locations=locations,
        salary_amount=salary_amount,
        salary_currency=salary_currency,
        salary_period=salary_period,
        submit_label="Update",
    )
    try:
        amount = _parse_amount(salary_amount)
    except InvalidOperation:
        return _page("Edit applicant", _form(**form_kwargs, error="salary amount must be a number"))

    try:
        pref = parse_preference_input(
            target_roles=_split_lines(target_roles),
            locations=_split_lines(locations),
            sector=sector,
            salary_amount=amount,
            salary_currency=(salary_currency or None) if amount is not None else None,
            salary_period=salary_period if amount is not None else None,
        )
        update_applicant(
            db,
            applicant_id,
            preference=pref,
            display_name=display_name,
            email=email,
        )
        db.commit()
    except ApplicantNotFoundError:
        db.rollback()
        return _page("Not found", "<p class='error'>applicant not found</p>")
    except (PreferenceValidationError, ApplicantConflictError) as exc:
        db.rollback()
        return _page("Edit applicant", _form(**form_kwargs, error=str(exc)))

    return RedirectResponse(
        url=f"/admin/applicants?admin_key={admin_key}",
        status_code=303,
    )
