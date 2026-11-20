"""Admin page for the monthly money cap. Until a cap is set, every paid call waits (D8)."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin
from app.api.admin_html import page
from app.api.chrome import admin_bar
from app.api.forms import csrf_field, require_csrf
from app.budget.policy import (
    ALL_PROVIDERS,
    CapError,
    active_caps,
    overall_cap,
    parse_cap,
    set_overall_cap,
    spent_this_month,
)
from app.core.db import get_db

router = APIRouter(prefix="/admin/budget", tags=["admin-budget"], dependencies=[Depends(require_admin)])

NO_CAP_WARNING = (
    "No monthly budget cap is set, so every paid call (Apify searches and AI) is waiting. "
    "Nothing is collected, judged or tailored until a cap is set."
)


def no_cap_banner(session: Session, now: datetime) -> str:
    """A warning for admin pages while no overall cap is set, linking here."""
    if overall_cap(session, now) is not None:
        return ""
    return f'<p class="error">{html.escape(NO_CAP_WARNING)} <a href="/admin/budget">Set the monthly cap</a>.</p>'


def _money(value: Decimal) -> str:
    return f"£{value:,.2f}" if value >= Decimal("0.01") or value == 0 else f"£{value:.4f}"


def _render(request: Request, db: Session, *, error: str = "", saved: bool = False, typed: str = "") -> str:
    now = datetime.now(UTC)
    cap = overall_cap(db, now)
    spent = spent_this_month(db, now)
    if cap is None:
        status = f'<p class="error">{html.escape(NO_CAP_WARNING)}</p>'
    elif cap == 0:
        status = "<p class=\"error\">The cap is £0, so every paid call is paused.</p>"
    else:
        left = max(cap - spent, Decimal(0))
        status = f"<p>Spent {_money(spent)} of {_money(cap)} this month. {_money(left)} left.</p>"
    others = [row for row in active_caps(db, now) if row.provider != ALL_PROVIDERS and row.money_cap is not None]
    other_html = ""
    if others:
        items = "".join(f"<li>{html.escape(row.provider)}: {_money(Decimal(row.money_cap))} a month</li>" for row in others)
        other_html = f"<h2>Caps for one provider</h2><p>These apply instead of the overall cap for that provider.</p><ul>{items}</ul>"
    notice = '<p>Saved. The new cap applies from now.</p>' if saved else ""
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    value = typed or ("" if cap is None else f"{cap:.2f}")
    return f"""<h1>Monthly budget</h1>
{notice}{err}{status}
<p>Spending counts from the 1st of each month (UTC). A paid call that would go over the cap waits until next month
or until the cap is raised; it is never marked as failed. Set £0 to pause all paid calls.</p>
<form method="post" action="/admin/budget">{csrf_field(request)}
  <label>Monthly cap (£) <input name="money_cap" inputmode="decimal" required value="{html.escape(value)}"/></label>
  <button type="submit">Save cap</button>
</form>
{other_html}"""


@router.get("", response_class=HTMLResponse)
def budget_page(request: Request, saved: int = 0, db: Session = Depends(get_db)) -> HTMLResponse:
    return page("Monthly budget", _render(request, db, saved=bool(saved)), header=admin_bar(request, "budget"))


@router.post("", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def budget_save(request: Request, money_cap: str = Form(""), db: Session = Depends(get_db)):
    try:
        amount = parse_cap(money_cap)
    except CapError as exc:
        body = _render(request, db, error=str(exc), typed=money_cap[:40])
        return page("Monthly budget", body, status_code=400, header=admin_bar(request, "budget"))
    set_overall_cap(db, amount, datetime.now(UTC))
    db.commit()
    return RedirectResponse("/admin/budget?saved=1", status_code=303)
