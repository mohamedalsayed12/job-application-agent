"""Create or refresh an Application: extract schema, map answers, set status."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.apply_answers import answers_as_map, load_answers
from app.applications.extract import extract_form_schema
from app.applications.mapper import build_fill_plan
from app.applications.schema import schema_to_json
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
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


async def prepare_application(
    session: Session,
    *,
    applicant: Applicant,
    job_id: uuid.UUID,
    gateway: FetchGateway,
    settings: Settings,
    persist,
    now=lambda: datetime.now(UTC),
) -> Application:
    job = session.get(Job, job_id)
    if job is None:
        raise PrepareRefused("That job was not found.")
    apply_url = (job.apply_url or "").strip()
    if not apply_url.lower().startswith(("http://", "https://")):
        raise PrepareRefused("This job has no usable apply link.")

    row = application_for_job(session, applicant.id, job_id)
    if row is None:
        row = Application(
            applicant_id=applicant.id,
            job_id=job_id,
            apply_url=apply_url,
            status=ApplicationStatus.preparing.value,
        )
        session.add(row)
    else:
        row.apply_url = apply_url
        row.status = ApplicationStatus.preparing.value
        row.error = None
        row.missing_fields = []
        row.schema_json = None
        row.fill_plan_json = None
    session.flush()
    persist()

    extracted = await extract_form_schema(
        session,
        apply_url=apply_url,
        gateway=gateway,
        settings=settings,
        persist=persist,
        now=now,
    )
    if extracted.budget_blocked:
        row.status = ApplicationStatus.failed.value
        row.error = extracted.error
        session.flush()
        return row
    if extracted.schema is None:
        row.status = ApplicationStatus.failed.value
        row.error = extracted.error or "Could not read the application form."
        session.flush()
        return row

    schema = extracted.schema
    row.ats_kind = schema.ats_kind.value
    row.schema_json = schema_to_json(schema)
    answers = answers_as_map(load_answers(session, applicant.id), email=applicant.email)
    plan = build_fill_plan(schema, answers)
    row.fill_plan_json = plan.to_json()
    row.missing_fields = plan.missing
    if plan.missing:
        row.status = ApplicationStatus.needs_input.value
        row.error = "Some required fields need answers on your apply profile."
    else:
        row.status = ApplicationStatus.ready_to_fill.value
        row.error = None
    session.flush()
    return row


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
