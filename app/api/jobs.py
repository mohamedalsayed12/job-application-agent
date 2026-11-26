"""One job's page. Opening it is what writes the job's summary (D24, D41)."""

from __future__ import annotations

import html
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.api.listings import place_names
from app.applications.prepare import application_for_job
from app.applications.ui import apply_step
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import (
    Applicant,
    ApplicantJobMatch,
    Job,
    JobFreshness,
    JobReport,
    TailoredCv,
    TailoredCvStatus,
    auto_tailored,
)
from app.summaries.generate import Shown, summary_for_view
from app.tailor.render import render_letter_docx, render_letter_pdf
from app.tailor.run import request_tailored_cv, tailor_allowed, tailors_outside_locations
from app.tailor.ui import changes_block, cover_letter_text, listing_tailor

router = APIRouter(prefix="/jobs", tags=["jobs"])

_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def get_gateway() -> AsyncIterator[FetchGateway]:
    gateway = FetchGateway()
    try:
        yield gateway
    finally:
        await gateway.aclose()


def _body(job: Job, shown: Shown, tailored: str, step: str) -> str:
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
<p><a href="/listings">← Back to your matches</a></p>
<div class="card">
<h1>{html.escape(job.title)}</h1>
<p>{html.escape(job.company_name)}. {html.escape(place_names(job))}.</p>
{about}
<p>Last confirmed open: {html.escape(last_seen)}.</p>
{tailored}
{step}
<p><a href="/jobs/{job.id}/interview">Interview practice for this job</a></p>
</div>
"""


def _tailored_for(db: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> TailoredCv | None:
    return db.scalars(
        select(TailoredCv).where(TailoredCv.applicant_id == applicant_id, TailoredCv.job_id == job_id)
    ).first()


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_page(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> HTMLResponse:
    job = db.get(Job, job_id)
    # D23: only open jobs are shown.
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status_code=404)
    settings = get_settings()
    shown = await summary_for_view(db, job, gateway=gateway, settings=settings, persist=db.commit)
    csrf = csrf_field(request)
    # Only a listed job in the person's tailoring scope is tailored without a click, so no other job says "queued".
    listed = db.execute(
        select(JobReport.verdict, ApplicantJobMatch.location_fit)
        .join(
            ApplicantJobMatch,
            (ApplicantJobMatch.applicant_id == JobReport.applicant_id) & (ApplicantJobMatch.job_id == JobReport.job_id),
        )
        .where(JobReport.applicant_id == applicant.id, JobReport.job_id == job_id)
    ).first()
    auto = listed is not None and auto_tailored(
        listed.verdict,
        location_fit=listed.location_fit,
        outside_locations=tailors_outside_locations(db, applicant.id),
    )
    row = _tailored_for(db, applicant.id, job_id)
    tailored = listing_tailor(
        job_id,
        row,
        csrf,
        auto=auto,
        allowed=tailor_allowed(settings, applicant.id),
    )
    if row is not None and row.status == TailoredCvStatus.ready.value:
        tailored += changes_block(row.ats_report or {})
    tailored += cover_letter_text(row)
    step = apply_step(job, application_for_job(db, applicant.id, job_id), csrf)
    return page(job.title, _body(job, shown, tailored, step), header=applicant_bar(request, "matches"), narrow=True)


@router.post("/{job_id}/tailor", dependencies=[Depends(require_csrf)])
def request_tailor(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    job = db.get(Job, job_id)
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status_code=404)
    if not tailor_allowed(get_settings(), applicant.id):
        raise HTTPException(status_code=403)
    request_tailored_cv(db, applicant.id, job_id)
    db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}/tailored.pdf")
def tailored_pdf(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    return _download(db, applicant.id, job_id, kind="pdf")


@router.get("/{job_id}/tailored.docx")
def tailored_docx(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    return _download(db, applicant.id, job_id, kind="docx")


@router.get("/{job_id}/cover-letter.pdf")
def cover_letter_pdf(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    return _download(db, applicant.id, job_id, kind="letter_pdf")


@router.get("/{job_id}/cover-letter.docx")
def cover_letter_docx(
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    return _download(db, applicant.id, job_id, kind="letter_docx")


def _download(db: Session, applicant_id: uuid.UUID, job_id: uuid.UUID, *, kind: str) -> Response:
    row = _tailored_for(db, applicant_id, job_id)
    if row is None or row.status != TailoredCvStatus.ready.value:
        raise HTTPException(status_code=404)
    if kind == "pdf":
        content, media, name = row.pdf_content, "application/pdf", "tailored-cv.pdf"
    elif kind == "docx":
        content, media, name = row.docx_content, _DOCX_TYPE, "tailored-cv.docx"
    elif not row.cover_letter:
        raise HTTPException(status_code=404)
    elif kind == "letter_pdf":
        content, media, name = render_letter_pdf(row.cover_letter, row.draft or {}), "application/pdf", "cover-letter.pdf"
    else:
        content, media, name = render_letter_docx(row.cover_letter, row.draft or {}), _DOCX_TYPE, "cover-letter.docx"
    if not content:
        raise HTTPException(status_code=404)
    return Response(
        content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
