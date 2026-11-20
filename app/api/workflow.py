"""The applicant's end-to-end workflow and the next useful action at every stage."""

from __future__ import annotations

import html
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.core.db import get_db
from app.matching.match import listed
from app.models import (
    Applicant,
    ApplicantJobMatch,
    ApplicantPreference,
    Application,
    ApplicationAnswers,
    ApplicationStatus,
    CvFile,
    ExtensionDevice,
    InterviewIntelRequest,
    InterviewJobPack,
    JobReport,
    OutreachContact,
    OutreachDraft,
    TailoredCv,
    TailoredCvStatus,
)

router = APIRouter(tags=["workflow"])


@dataclass(frozen=True, slots=True)
class Step:
    title: str
    detail: str
    status: str
    href: str
    action: str
    done: bool = False


def _count(db: Session, model: type, applicant_id: uuid.UUID, *conditions: object) -> int:
    statement = select(func.count()).select_from(model).where(model.applicant_id == applicant_id, *conditions)
    return int(db.scalar(statement) or 0)


def _latest_job_id(db: Session, model: type, applicant_id: uuid.UUID, *conditions: object) -> uuid.UUID | None:
    statement = (
        select(model.job_id)
        .where(model.applicant_id == applicant_id, *conditions)
        .order_by(model.updated_at.desc())
        .limit(1)
    )
    return db.scalar(statement)


def _listed_count(db: Session, applicant_id: uuid.UUID) -> int:
    """Matches on this person's listings: open, and in their locations (same as /listings)."""
    rows = listed().where(ApplicantJobMatch.applicant_id == applicant_id).subquery()
    return int(db.scalar(select(func.count()).select_from(rows)) or 0)


def _latest_listed_job_id(db: Session, applicant_id: uuid.UUID) -> uuid.UUID | None:
    statement = (
        listed()
        .where(ApplicantJobMatch.applicant_id == applicant_id)
        .order_by(ApplicantJobMatch.updated_at.desc())
        .limit(1)
    )
    row = db.execute(statement).first()
    return row[1].id if row else None


def _render_step(number: int, step: Step) -> str:
    state = "done" if step.done else ""
    badge = "Complete" if step.done else step.status
    return f"""
<article class="workflow-step {state}">
  <div class="workflow-number">{number}</div>
  <div class="workflow-copy">
    <h2>{html.escape(step.title)}</h2>
    <p>{html.escape(step.detail)}</p>
    <p><span class="tag {'good' if step.done else 'blue'}">{html.escape(badge)}</span></p>
  </div>
  <div class="workflow-action"><a class="button{' primary' if not step.done else ''}" href="{html.escape(step.href, quote=True)}">{html.escape(step.action)}</a></div>
</article>"""


@router.get("/workflow", response_class=HTMLResponse)
def workflow_page(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    applicant_id = applicant.id
    preference = db.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id))
    has_cv = db.scalar(select(CvFile.id).where(CvFile.applicant_id == applicant_id)) is not None
    profile_ready = preference is not None and has_cv

    matches = _listed_count(db, applicant_id)
    reports = _count(db, JobReport, applicant_id)
    tailored = _count(db, TailoredCv, applicant_id, TailoredCv.status == TailoredCvStatus.ready.value)
    latest_match = _latest_listed_job_id(db, applicant_id)

    has_answers = db.scalar(select(ApplicationAnswers.id).where(ApplicationAnswers.applicant_id == applicant_id)) is not None
    has_extension = db.scalar(select(ExtensionDevice.id).where(ExtensionDevice.applicant_id == applicant_id)) is not None
    applications = _count(db, Application, applicant_id)
    submitted = _count(db, Application, applicant_id, Application.status == ApplicationStatus.submitted.value)
    latest_applied = _latest_job_id(
        db, Application, applicant_id, Application.status == ApplicationStatus.submitted.value
    )

    contacts = _count(db, OutreachContact, applicant_id)
    drafts = _count(db, OutreachDraft, applicant_id)
    role_requests = _count(db, InterviewIntelRequest, applicant_id)
    job_packs = _count(db, InterviewJobPack, applicant_id)

    job_href = f"/jobs/{latest_match}" if latest_match else "/listings"
    outreach_href = f"/jobs/{latest_applied}/outreach" if latest_applied else "/applications"
    interview_href = f"/jobs/{latest_match}/interview" if latest_match else "/profile"
    setup_parts = []
    if not has_answers:
        setup_parts.append("apply answers")
    if not has_extension:
        setup_parts.append("extension token")

    steps = [
        Step(
            "Build your profile from your CV",
            "Set target roles, locations, experience levels and skills, then keep your master CV attached.",
            "Profile needs attention",
            "/profile",
            "Review profile" if profile_ready else "Complete profile",
            profile_ready,
        ),
        Step(
            "Find and rank matching jobs",
            f"{matches} current match{'es' if matches != 1 else ''}. Matching uses role, location, level, salary, skills, degrees and licences.",
            "Waiting for matches" if profile_ready else "Complete step 1 first",
            "/listings",
            "View matches",
            matches > 0,
        ),
        Step(
            "Review fit and tailored CV",
            f"{reports} fit report{'s' if reports != 1 else ''}; {tailored} tailored CV package{'s' if tailored != 1 else ''} ready. Each package is grounded in your master CV and checked for ATS text and keywords.",
            "Open a matched job",
            job_href,
            "Open job",
            reports > 0 and tailored > 0,
        ),
        Step(
            "Prepare and review the application",
            (
                f"{applications} application{'s' if applications != 1 else ''} prepared; {submitted} marked applied."
                if not setup_parts
                else "Before filling employer forms, set " + " and ".join(setup_parts) + "."
            ),
            "Application setup ready" if not setup_parts else "Setup required",
            "/applications" if not setup_parts else ("/profile/apply-answers" if not has_answers else "/account#extension"),
            "View applications" if not setup_parts else "Finish setup",
            applications > 0 and not setup_parts,
        ),
        Step(
            "Follow up after applying",
            f"{contacts} contact{'s' if contacts != 1 else ''} found; {drafts} reviewed outreach draft{'s' if drafts != 1 else ''}. Outreach unlocks only after an application is marked submitted.",
            "Apply to a job first" if submitted == 0 else "Ready for outreach",
            outreach_href,
            "Open outreach" if submitted else "View applications",
            drafts > 0,
        ),
        Step(
            "Prepare for interviews",
            f"{role_requests} role research request{'s' if role_requests != 1 else ''}; {job_packs} job-specific practice pack{'s' if job_packs != 1 else ''}. Packs combine public interview evidence, the advert and company research.",
            "Choose a role or matched job",
            interview_href,
            "Open interview prep",
            job_packs > 0,
        ),
    ]
    completed = sum(step.done for step in steps)
    body = f"""
<div class="page-head"><div>
  <h1>Your application workflow</h1>
  <p class="muted">One place to move from your CV to a matched job, application, outreach and interview practice.</p>
</div><span class="tag blue">{completed} of {len(steps)} stages active</span></div>
<div class="workflow">{''.join(_render_step(index, step) for index, step in enumerate(steps, 1))}</div>
"""
    return page("Your workflow", body, header=applicant_bar(request, "workflow"))
