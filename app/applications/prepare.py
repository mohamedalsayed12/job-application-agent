"""Create or refresh an Application and move it through its statuses."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.cv import base_cv
from app.applications.mapper import FillPlan
from app.models import Application, ApplicationStatus, Job


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


def prepare_application(session: Session, *, applicant_id: uuid.UUID, job_id: uuid.UUID) -> Application:
    """Ready the job for the extension, which reads the form from the page the person opens."""
    job = session.get(Job, job_id)
    if job is None:
        raise PrepareRefused("That job was not found.")
    apply_url = (job.apply_url or "").strip()
    if not apply_url.lower().startswith(("http://", "https://")):
        raise PrepareRefused("This job has no usable apply link.")

    row = application_for_job(session, applicant_id, job_id)
    if row is not None and row.status == ApplicationStatus.submitted.value:
        raise PrepareRefused("You have already applied for this job.")
    if row is None:
        row = Application(applicant_id=applicant_id, job_id=job_id, apply_url=apply_url)
        session.add(row)
    row.apply_url = apply_url
    row.schema_json = None
    store_fill_outcome(row, None, reopen=True)
    row.cv_file_id = cv.id if (cv := base_cv(session, applicant_id, job)) else None
    session.flush()
    return row


def store_fill_outcome(application: Application, plan: FillPlan | None, *, reopen: bool = False) -> None:
    """Write status, missing questions, and the fill plan together. A submitted application stays submitted."""
    if application.status == ApplicationStatus.submitted.value:
        return
    if plan is not None and plan.missing:
        shown = FillPlan(steps=[], missing=plan.missing, attach=plan.attach)
        application.status = ApplicationStatus.needs_input.value
        application.missing_fields = [question.label for question in plan.missing]
        application.fill_plan_json = shown.to_json()
        application.error = None
        return
    if reopen or application.status != ApplicationStatus.filled_awaiting_review.value:
        application.status = ApplicationStatus.ready_to_fill.value
    application.missing_fields = []
    application.fill_plan_json = plan.to_json() if plan is not None else None
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
        row = Application(applicant_id=applicant_id, job_id=job.id, apply_url=job.apply_url or "", status=ApplicationStatus.submitted.value)
        session.add(row)
    else:
        row.status = ApplicationStatus.submitted.value
        row.error = None
    session.flush()
    return row


def has_applied(session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    row = application_for_job(session, applicant_id, job_id)
    return row is not None and row.status == ApplicationStatus.submitted.value
