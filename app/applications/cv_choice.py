"""Which CV file an application uploads. One call site for the multi-cv change."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.cv import cv_for
from app.models import TailoredCv, TailoredCvStatus


@dataclass(frozen=True, slots=True)
class ChosenCv:
    content: bytes
    file_type: str


def choose_cv(session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID | None) -> ChosenCv | None:
    """The tailored PDF when that job has one ready, otherwise the uploaded CV."""
    if job_id is not None:
        tailored = session.scalar(
            select(TailoredCv).where(
                TailoredCv.applicant_id == applicant_id,
                TailoredCv.job_id == job_id,
                TailoredCv.status == TailoredCvStatus.ready.value,
            )
        )
        if tailored is not None and tailored.pdf_content:
            return ChosenCv(bytes(tailored.pdf_content), "pdf")
    cv = cv_for(session, applicant_id)
    if cv is None or not cv.content:
        return None
    return ChosenCv(bytes(cv.content), cv.file_type)
