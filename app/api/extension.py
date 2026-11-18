"""API for the browser extension: pull fill plans, CV bytes, report status."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.cv import application_cv
from app.applications.extension_device import applicant_for_token
from app.applications.fill_safety import assert_plan_has_no_submit_clicks
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    set_application_status,
)
from app.core.db import get_db
from app.models import ApplicationStatus, CvFileType, TailoredCv, TailoredCvStatus

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
    out = []
    for row in rows:
        plan = row.fill_plan_json or {"steps": [], "missing": []}
        assert_plan_has_no_submit_clicks(plan)
        out.append(
            {
                "id": str(row.id),
                "apply_url": row.apply_url,
                "ats_kind": row.ats_kind,
                "fill_plan": plan,
            }
        )
    return out


def _tailored_pdf(db: Session, applicant_id: uuid.UUID, application_id: uuid.UUID | None) -> bytes | None:
    """The job's tailored CV (D55) when one is ready, so the form gets the version written for it."""
    if application_id is None:
        return None
    row = get_application(db, application_id, applicant_id)
    if row is None:
        return None
    tailored = db.scalar(
        select(TailoredCv).where(
            TailoredCv.applicant_id == applicant_id,
            TailoredCv.job_id == row.job_id,
            TailoredCv.status == TailoredCvStatus.ready.value,
        )
    )
    return bytes(tailored.pdf_content) if tailored is not None and tailored.pdf_content else None


@router.get("/cv")
def download_cv(
    application_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> Response:
    tailored = _tailored_pdf(db, applicant_id, application_id)
    if tailored is not None:
        content, file_type = tailored, CvFileType.pdf.value
    else:
        application = get_application(db, application_id, applicant_id) if application_id else None
        cv = application_cv(db, applicant_id, application)
        if cv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No CV uploaded")
        content, file_type = bytes(cv.content), cv.file_type
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
