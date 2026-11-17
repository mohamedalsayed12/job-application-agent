"""API for the browser extension: pull fill plans, CV bytes, report status."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.applications.cv_choice import choose_cv
from app.applications.extension_device import applicant_for_token
from app.applications.mapper import FormField
from app.applications.plan import build_application_plan, notice_for
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    set_application_status,
)
from app.core.db import get_db
from app.models import Applicant, ApplicationStatus, CvFileType

router = APIRouter(prefix="/extension", tags=["extension"])


def _bearer_applicant(
    db: Session = Depends(get_db),
    authorization: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing extension token")
    token = authorization.split(" ", 1)[1].strip()
    applicant_id = applicant_for_token(db, token)
    if applicant_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid extension token")
    return applicant_id


class StatusBody(BaseModel):
    status: str = Field(min_length=1, max_length=64)
    error: str | None = Field(default=None, max_length=500)


@router.get("/applications/ready")
def ready_applications(
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = [
        row
        for row in list_applications(db, applicant_id)
        if row.status == ApplicationStatus.ready_to_fill.value
    ]
    return [{"id": str(row.id), "apply_url": row.apply_url} for row in rows]


class PlanBody(BaseModel):
    fields: list[FormField] = Field(min_length=1, max_length=80)


@router.post("/applications/{application_id}/plan")
async def plan_application(
    application_id: uuid.UUID,
    body: PlanBody,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> dict:
    row = get_application(db, application_id, applicant_id)
    applicant = db.get(Applicant, applicant_id)
    if row is None or applicant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status in (ApplicationStatus.submitted.value, ApplicationStatus.filled_awaiting_review.value):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This application is no longer being filled.")
    plan = await build_application_plan(db, applicant, row, body.fields)
    db.commit()
    return {"status": row.status, "plan": plan.to_json(), "notice": notice_for(plan)}


@router.get("/cv")
def download_cv(
    application_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> Response:
    job_id = None
    if application_id is not None:
        row = get_application(db, application_id, applicant_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
        job_id = row.job_id
    chosen = choose_cv(db, applicant_id, job_id)
    if chosen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No CV uploaded")
    content, file_type = chosen.content, chosen.file_type
    media = "application/pdf" if file_type == CvFileType.pdf.value else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    name = f"cv.{file_type}"
    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/applications/{application_id}/status")
def report_status(
    application_id: uuid.UUID,
    body: StatusBody,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> dict:
    row = get_application(db, application_id, applicant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        next_status = ApplicationStatus(body.status)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unknown status") from exc
    if next_status not in (
        ApplicationStatus.filled_awaiting_review,
        ApplicationStatus.needs_input,
        ApplicationStatus.failed,
        ApplicationStatus.abandoned,
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Status not allowed from extension")
    try:
        set_application_status(db, row, status=next_status, error=body.error)
    except PrepareRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=exc.message) from exc
    db.commit()
    return {"id": str(row.id), "status": row.status}
