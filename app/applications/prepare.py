"""Create or refresh an Application for fill-from-answers (D70 prepare; D66/D75 fill)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.cv import base_cv
from app.applications.mapper import FillPlan
from app.models import Applicant, Application, ApplicationStatus, Job


class PrepareRefused(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def get_application(session: Session, application_id: uuid.UUID, applicant_id: uuid.UUID) -> Application | None:
    row = session.get(Application, application_id)
    if row is None or row.applicant_id != applicant_id:
        return None
    return row


def application_for_job(session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> Application | None:
    return session.scalar(
        select(Application).where(Application.applicant_id == applicant_id, Application.job_id == job_id)
    )


def list_applications(session: Session, applicant_id: uuid.UUID) -> list[Application]:
    return list(
        session.scalars(
            select(Application)
            .where(Application.applicant_id == applicant_id)
            .order_by(Application.updated_at.desc())
        )
    )


def prepare_application(
    session: Session,
    *,
    applicant: Applicant,
    job_id: uuid.UUID,
) -> Application:
    """Ready to fill: the person opens the apply page and the extension reads the form there.

    Nothing is fetched here. The employer's form is often several clicks past the job board, needs
    the person logged in, or is built by JavaScript, so only their own browser sees it (D70).
    """
    job = session.get(Job, job_id)
    if job is None:
        raise PrepareRefused("That job was not found.")
    apply_url = (job.apply_url or "").strip()
    if not apply_url.lower().startswith(("http://", "https://")):
        raise PrepareRefused("This job has no usable apply link.")

    row = application_for_job(session, applicant.id, job_id)
    if row is not None and row.status == ApplicationStatus.submitted.value:
        raise PrepareRefused("You have already applied for this job.")
    if row is None:
        row = Application(
            applicant_id=applicant.id,
            job_id=job_id,
            apply_url=apply_url,
            status=ApplicationStatus.ready_to_fill.value,
        )
        session.add(row)
    else:
        row.apply_url = apply_url
    row.schema_json = None
    store_fill_outcome(row, None, reopen=True)
    row.cv_file_id = cv.id if (cv := base_cv(session, applicant.id, job)) else None
    session.flush()
    return row


def store_fill_outcome(
    application: Application,
    plan: FillPlan | None,
    *,
    reopen: bool = False,
    drafts: dict[str, Any] | None = None,
) -> None:
    """Write status, missing questions, and the fill plan together. A submitted application stays submitted.

    Steps stay on the plan even when some questions are still open (D75): the extension fills what it
    can, outlines AI drafts, and the person answers the rest.
    """
    if application.status == ApplicationStatus.submitted.value:
        return
    previous_drafts = ((application.fill_plan_json or {}).get("drafts") or {}) if drafts is None else drafts
    if plan is not None and plan.missing:
        application.status = ApplicationStatus.needs_input.value
        application.missing_fields = [question.label for question in plan.missing]
        application.fill_plan_json = {**plan.to_json(), "drafts": previous_drafts}
        application.error = None
        return
    if reopen or application.status != ApplicationStatus.filled_awaiting_review.value:
        application.status = ApplicationStatus.ready_to_fill.value
    application.missing_fields = []
    if plan is not None:
        application.fill_plan_json = {**plan.to_json(), "drafts": previous_drafts}
    else:
        application.fill_plan_json = None
    application.error = None


def set_application_status(
    session: Session,
    application: Application,
    *,
    status: ApplicationStatus,
    error: str | None = None,
) -> Application:
    if status is ApplicationStatus.filled_awaiting_review and application.missing_fields:
        raise PrepareRefused("Answer the missing questions first.")
    if status is ApplicationStatus.filled_awaiting_review and application.status not in (
        ApplicationStatus.ready_to_fill.value,
        ApplicationStatus.filled_awaiting_review.value,
        ApplicationStatus.needs_input.value,
    ):
        raise PrepareRefused("This application is not ready to mark as filled.")
    application.status = status.value
    if error is not None:
        application.error = error
    session.flush()
    return application


def mark_submitted(session: Session, applicant_id: uuid.UUID, job: Job) -> Application:
    """The person says they sent the application, here or on the employer's site directly."""
    row = application_for_job(session, applicant_id, job.id)
    if row is None:
        row = Application(
            applicant_id=applicant_id,
            job_id=job.id,
            apply_url=job.apply_url or "",
            status=ApplicationStatus.submitted.value,
        )
        session.add(row)
    else:
        row.status = ApplicationStatus.submitted.value
        row.error = None
    # D77: the worker now finds the hiring manager and people in this role and drafts outreach.
    if row.outreach_state is None:
        row.outreach_state = "queued"
    session.flush()
    return row


def has_applied(session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    row = application_for_job(session, applicant_id, job_id)
    return row is not None and row.status == ApplicationStatus.submitted.value
