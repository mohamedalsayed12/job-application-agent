"""Create or refresh an Application, and turn the form the extension read into a fill plan (D70)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.apply_answers import answers_as_map, load_answers
from app.applications.drafts import fill_from_bank, remember_unanswered
from app.applications.mapper import FillPlan, build_fill_plan
from app.applications.schema import (
    FormSchema,
    classify_ats,
    fields_from_scan,
    schema_to_json,
)
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
        row = Application(applicant_id=applicant.id, job_id=job_id, apply_url=apply_url, status=ApplicationStatus.ready_to_fill.value)
        session.add(row)
    else:
        row.apply_url = apply_url
        row.status = ApplicationStatus.ready_to_fill.value
        row.error = None
        row.missing_fields = []
        row.schema_json = None
        row.fill_plan_json = None
    session.flush()
    return row


def plan_for_scanned_form(
    session: Session,
    application: Application,
    applicant: Applicant,
    *,
    page_url: str,
    fields: list[dict[str, Any]],
) -> tuple[FormSchema, FillPlan, dict[str, object]]:
    """Map the fields the extension read to the person's saved answers, then to answers they gave on earlier forms.

    Returns the form, the plan and the saved answers. Nothing is kept until record_plan.
    """
    schema = fields_from_scan(fields, ats=classify_ats(page_url))
    answers = answers_as_map(load_answers(session, applicant.id), email=applicant.email)
    plan = fill_from_bank(session, applicant.id, schema, build_fill_plan(schema, answers))
    application.ats_kind = schema.ats_kind.value
    application.schema_json = {**schema_to_json(schema), "page_url": page_url}
    return schema, plan, answers


def record_plan(
    session: Session,
    application: Application,
    applicant: Applicant,
    schema: FormSchema,
    plan: FillPlan,
    drafts: dict[str, dict] | None = None,
) -> None:
    """Keeps the plan on the application, with every page's AI drafts (D74), and banks the required questions still open."""
    remember_unanswered(session, applicant.id, schema, plan)
    application.fill_plan_json = {**plan.to_json(), "drafts": drafts or {}}
    application.missing_fields = plan.missing
    if plan.missing:
        application.status = ApplicationStatus.needs_input.value
        application.error = "Some required questions on this form have no saved answer. They are listed below."
    else:
        application.status = ApplicationStatus.ready_to_fill.value
        application.error = None
    session.flush()


def set_application_status(
    session: Session,
    application: Application,
    *,
    status: ApplicationStatus,
    error: str | None = None,
) -> Application:
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
