"""One job's page. Opening it is what writes the job's summary (D24, D41)."""

from __future__ import annotations

import html
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.listings import place_names
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import Applicant, Job, JobFreshness
from app.summaries.generate import Shown, summary_for_view

router = APIRouter(prefix="/jobs", tags=["jobs"])


async def get_gateway() -> AsyncIterator[FetchGateway]:
    gateway = FetchGateway()
    try:
        yield gateway
    finally:
        await gateway.aclose()


def _body(job: Job, shown: Shown) -> str:
    if shown.summary:
        about = (
            f"<p>{html.escape(shown.summary)}</p>"
            "<p><small>Summary written by AI from this posting and checked against it. "
            "The full advert is at the apply link.</small></p>"
        )
    else:
        about = f'<div style="white-space: pre-line">{html.escape(job.description or "No description was given.")}</div>'
    last_seen = job.last_seen_at.strftime("%d %b %Y %H:%M UTC") if job.last_seen_at else "not recorded"
    return f"""
<h1>{html.escape(job.title)}</h1>
<p>{html.escape(job.company_name)}. {html.escape(place_names(job))}.</p>
{about}
<p>Last confirmed open: {html.escape(last_seen)}.</p>
{_apply_link(job.apply_url)}
"""


def _apply_link(url: str) -> str:
    """Only web links: an apply URL comes from outside data."""
    if not url.lower().startswith(("https://", "http://")):
        return "<p>No usable apply link was given.</p>"
    return f'<p><a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">Apply on the employer&#39;s site</a></p>'


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_page(
    job_id: uuid.UUID,
    _applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> HTMLResponse:
    job = db.get(Job, job_id)
    # D23: only open jobs are shown.
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status_code=404)
    shown = await summary_for_view(db, job, gateway=gateway, settings=get_settings(), persist=db.commit)
    return page(job.title, _body(job, shown))
