"""API for the browser extension: pull fill plans, CV bytes, report status."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.jobs import get_gateway
from app.applicants.apply_answers import document_file_name
from app.applicants.cv import cv_for
from app.applications.drafts import draft_with_ai
from app.applications.extension_device import applicant_for_token
from app.applications.fill_safety import assert_plan_has_no_submit_clicks
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    plan_for_scanned_form,
    record_plan,
    set_application_status,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import (
    Applicant,
    ApplicationStatus,
    CvFileType,
    Job,
    TailoredCv,
    TailoredCvStatus,
)

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


# Applications the extension may fill: prepared, waiting on answers the person has since given, or
# filled on one page with more pages of the form still to come.
_OPEN = (
    ApplicationStatus.ready_to_fill.value,
    ApplicationStatus.needs_input.value,
    ApplicationStatus.filled_awaiting_review.value,
)


@router.get("/applications/ready")
def ready_applications(
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Open applications, newest first, named so the person can pick the one for the open tab."""
    out = []
    for row in list_applications(db, applicant_id):
        if row.status not in _OPEN:
            continue
        job = db.get(Job, row.job_id)
        out.append(
            {
                "id": str(row.id),
                "apply_url": row.apply_url,
                "page_url": (row.schema_json or {}).get("page_url"),
                "title": job.title if job else "",
                "company": job.company_name if job else "",
                "status": row.status,
            }
        )
    return out


class ScannedField(BaseModel):
    label: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=300)
    type: str = Field(default="text", max_length=32)
    required: bool = False
    options: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=300)


class ScannedForm(BaseModel):
    page_url: str = Field(min_length=8, max_length=2000)
    fields: list[ScannedField] = Field(max_length=400)


@router.post("/applications/{application_id}/form")
async def plan_from_form(
    application_id: uuid.UUID,
    body: ScannedForm,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> dict:
    """The fields the extension read on the open page in, the fill plan out (D70).

    Saved answers first, then answers given on earlier forms, then AI drafts that were checked before
    they come back (D74). The extension highlights the drafts; the person reviews and submits.
    """
    row = get_application(db, application_id, applicant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status not in _OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This application is not open for filling")
    if not body.page_url.lower().startswith(("https://", "http://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Not a web page")
    applicant = db.get(Applicant, applicant_id)
    schema, plan, answers = plan_for_scanned_form(
        db, row, applicant, page_url=body.page_url, fields=[field.model_dump() for field in body.fields]
    )
    previous = (row.fill_plan_json or {}).get("drafts") if row.fill_plan_json else None
    plan, drafts = await draft_with_ai(
        db, applicant, db.get(Job, row.job_id), schema, plan, answers,
        gateway=gateway, settings=get_settings(), persist=db.commit, previous=previous,
    )
    record_plan(db, row, applicant, schema, plan, drafts)
    payload = plan.to_json()
    assert_plan_has_no_submit_clicks(payload)
    db.commit()
    return {"id": str(row.id), "status": row.status, **payload}



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
        cv = cv_for(db, applicant_id)
        if cv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No CV uploaded")
        content, file_type = bytes(cv.content), cv.file_type
    media = "application/pdf" if file_type == CvFileType.pdf.value else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Employers see this name on the uploaded file, so it is the person's name, not "cv.pdf".
    name = document_file_name(db, applicant_id, "CV", file_type)
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
