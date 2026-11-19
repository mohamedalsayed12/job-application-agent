"""One matched job's interview practice pack."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
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
from app.interview.roles import role_key
from app.interview.service import enqueue
from app.models import (
    Applicant,
    InterviewIntelRequest,
    InterviewJobPack,
    InterviewRequestStatus,
    Job,
    JobFreshness,
)

router = APIRouter(prefix="/jobs", tags=["interview-intel"])


def _job(db: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> tuple[Job, str] | None:
    job = db.get(Job, job_id)
    if job is None or job.freshness != JobFreshness.open:
        return None
    role = _matched_role(db, applicant_id, job)
    return None if role is None else (job, role)


def _not_available(request: Request) -> HTMLResponse:
    return page(
        "Interview practice",
        "<h1>Interview practice isn&#39;t available for this job</h1>"
        "<p>Practice packs are built for jobs that match your target roles, while they are still open.</p>"
        '<p><a href="/listings">Back to your matches</a></p>',
        status_code=404,
        header=applicant_bar(request, "matches"),
        narrow=True,
    )


def _research_in_progress(db: Session, applicant_id: uuid.UUID, role: str) -> bool:
    return (
        db.scalar(
            select(InterviewIntelRequest.id)
            .where(
                InterviewIntelRequest.applicant_id == applicant_id,
                InterviewIntelRequest.role_key == role_key(role),
                InterviewIntelRequest.status.in_(
                    (InterviewRequestStatus.queued.value, InterviewRequestStatus.running.value)
                ),
            )
            .limit(1)
        )
        is not None
    )


@router.get("/{job_id}/interview", response_class=HTMLResponse)
def interview_job_view(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    found = _job(db, applicant.id, job_id)
    if found is None:
        return _not_available(request)
    job, role = found
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
        waiting = (
            '<p class="notice">Role interview research is queued. Return after the worker finishes, '
            "then build this job's pack.</p>"
            if _research_in_progress(db, applicant.id, role)
            else ""
        )
        body = heading + waiting + '<p>Build a pack from this advert and public interview stories.</p>' + form
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
    found = _job(db, applicant.id, job_id)
    if found is None:
        return _not_available(request)
    job, role = found
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
