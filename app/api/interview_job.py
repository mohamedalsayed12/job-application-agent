"""One matched job's interview practice pack."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.api.interview_intel import _materials, _plan, _section, _themes
from app.api.jobs import get_gateway
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.interview.job_pack import _matched_role, build_job_pack
from app.interview.service import enqueue
from app.models import Applicant, InterviewJobPack, Job, JobFreshness

router = APIRouter(prefix="/jobs", tags=["interview-intel"])


def _job(db: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> tuple[Job, str]:
    job = db.get(Job, job_id)
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status_code=404)
    role = _matched_role(db, applicant_id, job)
    if role is None:
        raise HTTPException(status_code=404)
    return job, role


@router.get("/{job_id}/interview", response_class=HTMLResponse)
def interview_job_view(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job, _ = _job(db, applicant.id, job_id)
    pack = db.scalar(
        select(InterviewJobPack)
        .where(InterviewJobPack.applicant_id == applicant.id, InterviewJobPack.job_id == job_id)
        .order_by(InterviewJobPack.ready_at.desc())
        .limit(1)
    )
    heading = f"<h1>Interview practice: {html.escape(job.title)}</h1><p>{html.escape(job.company_name)}</p>"
    form = (
        f'<form method="post" action="/jobs/{job_id}/interview">{csrf_field(request)}'
        '<button type="submit" class="primary">Build or refresh practice pack</button></form>'
    )
    if pack is None or pack.expires_at <= datetime.now(UTC):
        body = heading + '<p>Build a pack from this advert and public interview stories.</p>' + form
    else:
        body = (
            heading
            + _section("Findings summary", f"<p>{html.escape(pack.summary)}</p>")
            + _themes("Why people succeeded", pack.success_themes)
            + _themes("Why people failed", pack.failure_themes)
            + _themes("Differentiators", pack.differentiators)
            + _materials(pack.materials)
            + _plan(pack.practice_plan)
            + form
        )
    body += f'<p><a href="/jobs/{job_id}">Back to job</a></p>'
    return page("Interview practice", body, header=applicant_bar(request, "matches"), narrow=True)


@router.post("/{job_id}/interview", response_model=None, dependencies=[Depends(require_csrf)])
async def interview_job_create(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> HTMLResponse | RedirectResponse:
    job, role = _job(db, applicant.id, job_id)
    result = await build_job_pack(
        db, applicant_id=applicant.id, job=job, gateway=gateway, settings=get_settings()
    )
    if result.stopped_by == "role_pack_missing":
        enqueue(db, applicant_id=applicant.id, role=role)
        db.commit()
        message = "Role interview research is queued. Return after the worker finishes, then build this job's pack."
    elif result.pack is not None:
        return RedirectResponse(f"/jobs/{job_id}/interview", status_code=303)
    else:
        message = {
            "budget": "The spending limit has been reached. Try again after the budget resets.",
            "daily token cap": "The daily AI limit has been reached. Try again tomorrow.",
            "not_configured": "Interview research is not configured yet.",
            "unsafe_advert": "This advert could not be used safely for interview research.",
            "ungrounded": "The generated pack could not be verified against the sources. Try again later.",
        }.get(result.stopped_by or "", "The pack could not be built. Try again later.")
    body = (
        f'<h1>Interview practice: {html.escape(job.title)}</h1><p>{html.escape(message)}</p>'
        f'<p><a href="/jobs/{job_id}/interview">Back to interview practice</a></p>'
    )
    return page("Interview practice", body, header=applicant_bar(request, "matches"), narrow=True)
