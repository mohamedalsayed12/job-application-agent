"""Admin review list: approve, reject, or mark unsupported with a reason."""

from __future__ import annotations

import html
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin_key
from app.core.db import get_db
from app.models import ReviewDecision
from app.verify.review import (
    ReviewConflictError,
    ReviewNotFoundError,
    apply_review_decision,
    list_open_reviews,
)

router = APIRouter(prefix="/admin/review", tags=["admin-review"])


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 56rem; }}
    label {{ display: block; margin-top: 0.5rem; }}
    input, textarea, select {{ width: 100%; box-sizing: border-box; }}
    .error {{ color: #a00; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border-bottom: 1px solid #ccc; text-align: left; padding: 0.4rem; vertical-align: top; }}
    .actions {{ min-width: 14rem; }}
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
def review_index(
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    rows = list_open_reviews(db)
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    items: list[str] = []
    for item, candidate in rows:
        board = (
            f"{candidate.board_system}/{candidate.board_instance}/{candidate.board_id}"
            if candidate.board_system
            else "(none)"
        )
        form_action = _action(f"/admin/review/{item.id}/decide", admin_key)
        items.append(
            "<tr>"
            f"<td>{html.escape(candidate.company_name)}</td>"
            f"<td>{html.escape(candidate.primary_domain or '')}</td>"
            f"<td>{html.escape(board)}</td>"
            f"<td>{html.escape(item.reason)}</td>"
            f"<td>{item.priority}</td>"
            f'<td class="actions">'
            f'<form method="post" action="{html.escape(form_action)}">'
            f'<label>Decision'
            f'<select name="decision" required>'
            f'<option value="{ReviewDecision.verified.value}">approve (verified)</option>'
            f'<option value="{ReviewDecision.rejected.value}">reject</option>'
            f'<option value="{ReviewDecision.unsupported.value}">unsupported</option>'
            f"</select></label>"
            f'<label>Reason'
            f'<textarea name="reason" rows="2" required></textarea></label>'
            f'<button type="submit">Save</button>'
            f"</form></td>"
            "</tr>"
        )
    body = f"""
<h1>Review queue</h1>
{err}
<p>Open needs_review items. Only reviewers can reject.</p>
<table>
  <thead>
    <tr>
      <th>Company</th><th>Domain</th><th>Board</th>
      <th>Why queued</th><th>Priority</th><th>Decide</th>
    </tr>
  </thead>
  <tbody>
    {"".join(items) if items else "<tr><td colspan='6'>No items need review.</td></tr>"}
  </tbody>
</table>
"""
    return _page("Review queue", body)


@router.post("/{review_item_id}/decide", response_class=HTMLResponse)
def review_decide(
    review_item_id: uuid.UUID,
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        apply_review_decision(
            db,
            review_item_id,
            decision=decision,
            reason=reason,
            reviewer="admin",
        )
        db.commit()
    except ReviewNotFoundError:
        db.rollback()
        return _page("Not found", "<p class='error'>review item not found</p>")
    except ReviewConflictError as exc:
        db.rollback()
        return RedirectResponse(
            url=_action("/admin/review", admin_key) + f"&error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=_action("/admin/review", admin_key),
        status_code=303,
    )
