"""API for the browser extension: pull fill plans, CV bytes, report status."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.jobs import get_gateway
from app.applications.cv_choice import choose_cv
from app.applications.extension_device import applicant_for_token
from app.applications.fill_safety import assert_plan_has_no_submit_clicks
from app.applications.mapper import FormField
from app.applications.plan import build_application_plan, notice_for
from app.applications.prepare import (
    PrepareRefused,
    get_application,
    list_applications,
    set_application_status,
)
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import Applicant, ApplicationStatus, CvFileType, Job

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
    id: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=300)
    type: str = Field(default="text", max_length=32)
    required: bool = False
    options: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=400)


class ScannedForm(BaseModel):
    page_url: str = Field(min_length=8, max_length=2000)
    fields: list[ScannedField] = Field(max_length=400)


_SCAN_TYPES = {
    "text": "text",
    "email": "email",
    "tel": "tel",
    "url": "url",
    "number": "number",
    "date": "date",
    "month": "month",
    "search": "text",
    "textarea": "textarea",
    "select": "select",
    "select-one": "select",
    "select-multiple": "select",
    "radio": "radio",
    "checkbox": "checkbox",
    "file": "file",
    # Custom dropdowns (Workday, Greenhouse, Ashby): options are read once opened (D75).
    "combobox": "select",
}


def _as_form_fields(items: list[ScannedField]) -> list[FormField]:
    fields: list[FormField] = []
    for index, item in enumerate(items):
        field_type = _SCAN_TYPES.get(str(item.type or "text").lower())
        if field_type is None:
            continue
        label = item.label.strip()
        name = item.name.strip()
        if not (label or name):
            continue
        field_id = item.id.strip() or f"f{index}"
        fields.append(
            FormField(
                id=field_id,
                label=label or name,
                name=name or label,
                type=field_type,  # type: ignore[arg-type]
                required=item.required,
                options=[option.strip() for option in item.options if option.strip()],
            )
        )
    return fields


@router.post("/applications/{application_id}/form")
async def plan_from_form(
    application_id: uuid.UUID,
    body: ScannedForm,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
) -> dict:
    """The fields the extension read on the open page in, the fill plan out (D70 + D66 + D75).

    Saved answers and CV quotes first, then the question bank, then AI drafts that were checked before
    they come back. The extension highlights the drafts; the person reviews and submits.
    """
    row = get_application(db, application_id, applicant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status not in _OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This application is not open for filling")
    if not body.page_url.lower().startswith(("https://", "http://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Not a web page")
    applicant = db.get(Applicant, applicant_id)
    if applicant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    fields = _as_form_fields(body.fields)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No form fields on this page")
    plan = await build_application_plan(db, applicant, row, fields, gateway=gateway)
    row.schema_json = {
        "page_url": body.page_url,
        "source": "extension",
        "fields": [
            {
                "id": field.id,
                "label": field.label,
                "name": field.name,
                "type": field.type,
                "required": field.required,
                "options": field.options,
            }
            for field in fields
        ],
    }
    payload = plan.to_json()
    assert_plan_has_no_submit_clicks(payload)
    db.commit()
    return {
        "id": str(row.id),
        "status": row.status,
        "plan": payload,
        "notice": notice_for(plan),
        **payload,
    }


@router.get("/cv")
def download_cv(
    application_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID = Depends(_bearer_applicant),
    db: Session = Depends(get_db),
) -> Response:
    row = None
    if application_id is not None:
        row = get_application(db, application_id, applicant_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found")
    chosen = choose_cv(db, applicant_id, row)
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
