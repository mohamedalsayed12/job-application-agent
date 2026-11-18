"""Which CV file an application uploads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.cv import application_cv
from app.models import Application, TailoredCv, TailoredCvStatus


@dataclass(frozen=True, slots=True)
class ChosenCv:
    content: bytes
    file_type: str


def choose_cv(session: Session, applicant_id: uuid.UUID, application: Application | None) -> ChosenCv | None:
    """The tailored PDF when the application's job has one ready, otherwise the CV chosen for it (D65)."""
    if application is not None:
        tailored = session.scalar(
            select(TailoredCv).where(
                TailoredCv.applicant_id == applicant_id,
                TailoredCv.job_id == application.job_id,
                TailoredCv.status == TailoredCvStatus.ready.value,
            )
        )
        if tailored is not None and tailored.pdf_content:
            return ChosenCv(bytes(tailored.pdf_content), "pdf")
    cv = application_cv(session, applicant_id, application)
    if cv is None or not cv.content:
        return None
    return ChosenCv(bytes(cv.content), cv.file_type)
