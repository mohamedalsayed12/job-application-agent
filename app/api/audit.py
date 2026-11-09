"""Admin audit sheet: sample classified companies and download the operator CSV."""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin
from app.api.admin_html import page
from app.api.chrome import admin_bar
from app.api.forms import csrf_field, require_csrf
from app.audit.csv_export import csv_text
from app.audit.sample import InsufficientClassifiedError, create_audit_sheet
from app.audit.types import DEFAULT_SAMPLE_SIZE, MAX_SAMPLE_SIZE
from app.core.db import get_db

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"], dependencies=[Depends(require_admin)])


@router.get("")
def audit_index(
    request: Request,
    error: str | None = Query(default=None),
):
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = f"""
<h1>Classification audit</h1>
{err}
<p>Samples classified companies (<code>verified</code>, <code>needs_review</code>,
<code>unsupported</code>) that are not already on an audit sheet, writes
<code>audit_sample</code> rows, and downloads a CSV for you to judge.
The system does not fill auditor columns.</p>
<form class="card" method="post" action="/admin/audit/sample">
  {csrf_field(request)}
  <label>Sample size
    <input type="number" name="size" min="1" max="{MAX_SAMPLE_SIZE}"
      value="{DEFAULT_SAMPLE_SIZE}" required/>
  </label>
  <p><button type="submit" class="primary">Create sample and download CSV</button></p>
</form>
"""
    return page("Classification audit", body, header=admin_bar(request, "audit"))


@router.post("/sample", response_model=None, dependencies=[Depends(require_csrf)])
def audit_sample(
    size: int = Form(default=DEFAULT_SAMPLE_SIZE),
    db: Session = Depends(get_db),
) -> PlainTextResponse | RedirectResponse:
    try:
        rows, summary = create_audit_sheet(db, size=size)
        db.commit()
    except (ValueError, InsufficientClassifiedError) as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/audit?error={quote(str(exc))}",
            status_code=303,
        )
    meta = (
        f"selected={summary.selected};"
        + ";".join(f"{state}={count}" for state, count in summary.by_state.items())
        + ";available="
        + ";".join(
            f"{state}:{count}"
            for state, count in summary.classified_available.items()
        )
    )
    return PlainTextResponse(
        csv_text(rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="step10-audit-sample.csv"',
            "X-Audit-Summary": meta,
        },
    )
