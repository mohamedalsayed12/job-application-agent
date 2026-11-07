"""Admin audit sheet: sample classified companies and download the operator CSV."""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin_key
from app.api.admin_html import action, page
from app.audit.csv_export import csv_text
from app.audit.sample import InsufficientClassifiedError, create_audit_sheet
from app.audit.types import DEFAULT_SAMPLE_SIZE, MAX_SAMPLE_SIZE
from app.core.db import get_db

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


@router.get("")
def audit_index(
    admin_key: str = Depends(require_admin_key),
    error: str | None = Query(default=None),
):
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    form_action = action("/admin/audit/sample", admin_key)
    body = f"""
<h1>Classification audit</h1>
{err}
<p>Samples classified companies (<code>verified</code>, <code>needs_review</code>,
<code>unsupported</code>) that are not already on an audit sheet, writes
<code>audit_sample</code> rows, and downloads a CSV for you to judge.
The system does not fill auditor columns.</p>
<form method="post" action="{html.escape(form_action)}">
  <label>Sample size
    <input type="number" name="size" min="1" max="{MAX_SAMPLE_SIZE}"
      value="{DEFAULT_SAMPLE_SIZE}" required/>
  </label>
  <button type="submit">Create sample and download CSV</button>
</form>
"""
    return page("Classification audit", body)


@router.post("/sample", response_model=None)
def audit_sample(
    size: int = Form(default=DEFAULT_SAMPLE_SIZE),
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> PlainTextResponse | RedirectResponse:
    try:
        rows, summary = create_audit_sheet(db, size=size)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            url=action("/admin/audit", admin_key) + f"&error={quote(str(exc))}",
            status_code=303,
        )
    except InsufficientClassifiedError as exc:
        db.rollback()
        return RedirectResponse(
            url=action("/admin/audit", admin_key) + f"&error={quote(str(exc))}",
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
