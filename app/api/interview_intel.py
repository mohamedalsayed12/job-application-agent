"""On-demand interview intel from Profile: generate and view a role pack."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.core.db import get_db
from app.interview.roles import role_key
from app.interview.service import enqueue
from app.models import (
    Applicant,
    ApplicantSkill,
    InterviewIntelPack,
    InterviewIntelRequest,
    InterviewRequestStatus,
)

router = APIRouter(prefix="/profile", tags=["interview-intel"])
_SEE_OTHER = status.HTTP_303_SEE_OTHER
_CSRF = [Depends(require_csrf)]


def interview_card(request: Request, roles: list[str]) -> str:
    if not roles:
        return ""
    options = "".join(f'<option value="{html.escape(role)}">{html.escape(role)}</option>' for role in roles)
    return f"""
<section class="card">
  <h2>Interview intel</h2>
  <p class="hint">We look at public Reddit, YouTube and LinkedIn stories about interviews for one of your target
  roles, compare what worked with what did not, and build a practice plan. This can take a few minutes after you ask.</p>
  <form method="post" action="/profile/interview-intel">
    {csrf_field(request)}
    <label>Target role
      <select name="role" required>{options}</select>
    </label>
    <button type="submit" class="primary">Generate interview intel</button>
  </form>
</section>
"""


@router.post("/interview-intel", response_model=None, dependencies=_CSRF)
def interview_intel_create(
    request: Request,
    role: str = Form(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        request_row = enqueue(db, applicant_id=applicant.id, role=role)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return page(
            "Interview intel",
            f'<h1>Interview intel</h1><p class="error">{html.escape(str(exc))}</p>'
            f'<p><a href="/profile">Back to your profile</a></p>',
            status_code=400,
            header=applicant_bar(request, "profile"),
            narrow=True,
        )
    return RedirectResponse(f"/profile/interview-intel?role={quote(request_row.role)}", status_code=_SEE_OTHER)


@router.get("/interview-intel", response_class=HTMLResponse)
def interview_intel_view(
    request: Request,
    role: str = Query(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    key = role_key(role)
    row = db.scalar(
        select(InterviewIntelRequest)
        .where(
            InterviewIntelRequest.applicant_id == applicant.id,
            InterviewIntelRequest.role_key == key,
        )
        .order_by(InterviewIntelRequest.created_at.desc())
        .limit(1)
    )
    header = applicant_bar(request, "profile")
    if row is None:
        return page(
            "Interview intel",
            f'<h1>Interview intel</h1><p>No request yet for {html.escape(role)}.</p>'
            f'<p><a href="/profile">Back to your profile</a></p>',
            header=header,
            narrow=True,
        )
    if row.status in {InterviewRequestStatus.queued.value, InterviewRequestStatus.running.value}:
        return page(
            "Interview intel",
            f"""
<h1>Interview intel</h1>
<p class="muted">{html.escape(row.role)}</p>
<div class="card">
  <p>We're gathering public stories and building your plan. This page will show the results when the worker finishes.
  Refresh in a minute or two.</p>
  <p class="hint">Status: {html.escape(row.status)}</p>
</div>
<p><a href="/profile">Back to your profile</a></p>
""",
            header=header,
            narrow=True,
        )
    if row.status == InterviewRequestStatus.failed.value:
        return page(
            "Interview intel",
            f"""
<h1>Interview intel</h1>
<p class="muted">{html.escape(row.role)}</p>
<div class="card">
  <p class="error">We couldn't build a pack this time{': ' + html.escape(row.error) if row.error else ''}.</p>
  <form method="post" action="/profile/interview-intel">{csrf_field(request)}
    <input type="hidden" name="role" value="{html.escape(row.role)}"/>
    <button type="submit" class="primary">Try again</button>
  </form>
</div>
<p><a href="/profile">Back to your profile</a></p>
""",
            header=header,
            narrow=True,
        )
    pack = db.get(InterviewIntelPack, row.pack_id) if row.pack_id else None
    if pack is None or pack.expires_at <= datetime.now(UTC):
        return page(
            "Interview intel",
            f"""
<h1>Interview intel</h1>
<p class="muted">{html.escape(row.role)}</p>
<div class="card"><p>This pack has expired. Generate it again from your profile.</p></div>
<p><a href="/profile">Back to your profile</a></p>
""",
            header=header,
            narrow=True,
        )
    skills = [
        skill.text_as_typed
        for skill in db.scalars(select(ApplicantSkill).where(ApplicantSkill.applicant_id == applicant.id))
    ]
    for_you = ""
    if skills:
        shown = ", ".join(html.escape(name) for name in skills[:8])
        for_you = (
            f'<p class="hint">From your profile skills ({shown}): practise STAR stories that use these, '
            "without inventing experience you do not have.</p>"
        )
    body = f"""
<h1>Interview intel</h1>
<p class="muted">{html.escape(pack.role_label)} · ready {_when(pack.ready_at)} · expires {_when(pack.expires_at)}</p>
{for_you}
{_section("Findings summary", f"<p>{html.escape(pack.summary)}</p>")}
{_themes("Why people succeeded", pack.success_themes)}
{_themes("Why people failed", pack.failure_themes)}
{_themes("Differentiators", pack.differentiators)}
{_materials(pack.materials)}
{_plan(pack.practice_plan)}
<p><a href="/profile">Back to your profile</a></p>
"""
    return page("Interview intel", body, header=header, narrow=True)


def _when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d %b %Y")


def _section(title: str, inner: str) -> str:
    return f'<section class="card"><h2>{html.escape(title)}</h2>{inner}</section>'


def _themes(title: str, themes: list[Any]) -> str:
    if not themes:
        return _section(title, "<p class=\"hint\">Nothing clear enough in the sources.</p>")
    items = []
    for theme in themes:
        if not isinstance(theme, dict):
            continue
        cites = "".join(
            f'<li><a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{html.escape(url)}</a></li>'
            for url in theme.get("cites") or []
            if isinstance(url, str)
        )
        label = html.escape(str(theme.get("label") or "").strip())
        detail = html.escape(str(theme.get("detail") or ""))
        shown = f"{label} {detail}".strip() if label else detail
        items.append(
            f"<li><strong>{html.escape(str(theme.get('theme') or ''))}</strong> — "
            f"{shown}"
            f"{'<ul>' + cites + '</ul>' if cites else ''}</li>"
        )
    return _section(title, f"<ul>{''.join(items)}</ul>" if items else "<p class=\"hint\">Nothing clear enough.</p>")


def _materials(materials: list[Any]) -> str:
    if not materials:
        return _section("Materials and resources", "<p class=\"hint\">No named materials in the sources.</p>")
    items = []
    for material in materials:
        if not isinstance(material, dict):
            continue
        url = str(material.get("url") or "")
        name = html.escape(str(material.get("name") or url))
        how = html.escape(str(material.get("how_used") or ""))
        link = f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{name}</a>' if url else name
        items.append(f"<li>{link} — {how}</li>")
    return _section("Materials and resources", f"<ul>{''.join(items)}</ul>")


def _plan(steps: list[Any]) -> str:
    if not steps:
        return _section("Practice plan", "<p class=\"hint\">No practice steps could be grounded.</p>")
    ordered = sorted(
        (step for step in steps if isinstance(step, dict)),
        key=lambda step: int(step.get("order") or 0),
    )
    items = []
    for step in ordered:
        resources = "".join(
            f'<li><a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{html.escape(url)}</a></li>'
            for url in step.get("resource_urls") or []
            if isinstance(url, str)
        )
        items.append(
            f"<li><strong>{html.escape(str(step.get('method') or ''))}</strong> — "
            f"{html.escape(str(step.get('detail') or ''))}"
            f"{'<ul>' + resources + '</ul>' if resources else ''}</li>"
        )
    return _section("Practice plan", f"<ol>{''.join(items)}</ol>")
