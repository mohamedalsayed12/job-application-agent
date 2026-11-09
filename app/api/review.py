"""Admin review list: approve, reject, or mark unsupported with a reason."""

from __future__ import annotations

import html
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin
from app.api.admin_html import page as _page
from app.api.chrome import admin_bar
from app.api.forms import csrf_field, require_csrf
from app.core.db import get_db
from app.models import ReviewDecision
from app.verify.review import (
    ReviewConflictError,
    ReviewNotFoundError,
    apply_review_decision,
    list_open_reviews,
)

router = APIRouter(prefix="/admin/review", tags=["admin-review"], dependencies=[Depends(require_admin)])


_STYLE = """<style>
.review { display: grid; grid-template-columns: 1fr minmax(15rem, 22rem); gap: 1rem; }
.review h2 { margin: 0; font-size: 1rem; }
.review .meta { font-size: 0.85rem; color: var(--muted); margin: 0.2rem 0; }
.review textarea { margin-bottom: 0.5rem; }
.decide { display: flex; gap: 0.4rem; flex-wrap: wrap; }
@media (max-width: 40rem) { .review { grid-template-columns: 1fr; } }
</style>"""


def _review_card(request: Request, item: object, candidate: object) -> str:
    board = (
        f"{candidate.board_system}/{candidate.board_instance}/{candidate.board_id}"
        if candidate.board_system
        else "(none)"
    )
    return f"""
<article class="card review">
  <div>
    <h2>{html.escape(candidate.company_name)}</h2>
    <p class="meta">{html.escape(candidate.primary_domain or "no domain")} · Board <code>{html.escape(board)}</code> · Priority {item.priority}</p>
    <p><b>Why queued:</b> {html.escape(item.reason)}</p>
  </div>
  <form method="post" action="/admin/review/{item.id}/decide">{csrf_field(request)}
    <label>Reason for your decision<textarea name="reason" rows="2" required></textarea></label>
    <div class="decide">
      <button type="submit" class="primary" name="decision" value="{ReviewDecision.verified.value}">Approve</button>
      <button type="submit" name="decision" value="{ReviewDecision.unsupported.value}">Unsupported</button>
      <button type="submit" class="danger" name="decision" value="{ReviewDecision.rejected.value}">Reject</button>
    </div>
  </form>
</article>"""


@router.get("", response_class=HTMLResponse)
def review_index(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    rows = list_open_reviews(db)
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    items = "".join(_review_card(request, item, candidate) for item, candidate in rows)
    body = f"""{_STYLE}
<div class="page-head"><div>
  <h1>Review queue</h1>
  <p class="muted">{len(rows)} open needs_review items. Only reviewers can reject. Every decision needs a reason.</p>
</div></div>
{err}
{items or '<div class="card"><p>No items need review.</p></div>'}
"""
    return _page("Review queue", body, header=admin_bar(request, "review"))


@router.post("/{review_item_id}/decide", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def review_decide(
    review_item_id: uuid.UUID,
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()],
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
            url=f"/admin/review?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/review",
        status_code=303,
    )
