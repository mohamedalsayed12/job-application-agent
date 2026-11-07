"""Admin audit sheet: sample classified companies and download the operator CSV."""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin_key
from app.audit.csv_export import csv_text
from app.audit.sample import InsufficientClassifiedError, create_audit_sheet
from app.audit.types import DEFAULT_SAMPLE_SIZE
from app.core.db import get_db

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem; }}
    label {{ display: block; margin-top: 0.5rem; }}
    .error {{ color: #a00; }}
    code {{ background: #f4f4f4; padding: 0.1rem 0.3rem; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""
    return HTMLResponse(doc)


def _action(path: str, admin_key: str) -> str:
    return f"{path}?admin_key={quote(admin_key, safe='')}"


@router.get("", response_class=HTMLResponse)
def audit_index(
    admin_key: str = Depends(require_admin_key),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    form_action = _action("/admin/audit/sample", admin_key)
    body = f"""
<h1>Classification audit</h1>
{err}
<p>Samples classified companies (<code>verified</code>, <code>needs_review</code>,
<code>unsupported</code>), writes <code>audit_sample</code> rows, and downloads a CSV
for you to judge. The system does not fill auditor columns.</p>
<form method="post" action="{html.escape(form_action)}">
  <label>Sample size
    <input type="number" name="size" min="1" max="200" value="{DEFAULT_SAMPLE_SIZE}" required/>
  </label>
  <button type="submit">Create sample and download CSV</button>
</form>
"""
    return _page("Classification audit", body)


@router.post("/sample", response_model=None)
def audit_sample(
    size: int = Form(default=DEFAULT_SAMPLE_SIZE),
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> PlainTextResponse | RedirectResponse:
    if size < 1 or size > 200:
        return RedirectResponse(
            url=_action("/admin/audit", admin_key)
            + f"&error={quote('size must be 1–200')}",
            status_code=303,
        )
    try:
        rows, _summary = create_audit_sheet(db, size=size)
        db.commit()
    except InsufficientClassifiedError as exc:
        db.rollback()
        return RedirectResponse(
            url=_action("/admin/audit", admin_key) + f"&error={quote(str(exc))}",
            status_code=303,
        )
    return PlainTextResponse(
        csv_text(rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="step10-audit-sample.csv"'
        },
    )
