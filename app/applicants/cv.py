"""A person's CVs: check, store, replace, label and delete them, and pick the one a job gets (D38, D65)."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applicants.profile import MAX_ITEM_LENGTH
from app.models import Applicant, Application, CvFile, CvFileType, Job

MAX_CV_BYTES = 5 * 1024 * 1024
# Room for the profile form's fields and part headers around a file of the largest size.
MAX_UPLOAD_BODY_BYTES = MAX_CV_BYTES + 64 * 1024
MAX_CVS = 10
MEDIA_TYPES = {
    CvFileType.pdf: "application/pdf",
    CvFileType.docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_MAX_ZIP_ENTRIES = 1000
_MAX_UNPACKED_BYTES = 100 * 1024 * 1024
_MAX_CONTENT_TYPES_BYTES = 1024 * 1024
_CONTENT_TYPES = "[Content_Types].xml"
_WORD_DOCUMENT = "word/document.xml"
_WORD_MAIN_TYPE = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MACRO_MARKER = b"macroenabled"
_MACRO_PART = "vbaproject.bin"
_NOT_A_CV_FILE = "Upload a PDF or a Word document (.docx)."


class CvRefused(ValueError):
    """A problem with the file that the person can read and act on."""


def check_cv(content: bytes | None) -> CvFileType:
    """The type comes from the file's own bytes, never from its name or the browser's label."""
    if not content:
        raise CvRefused("Choose a PDF or Word (.docx) file to upload.")
    if len(content) > MAX_CV_BYTES:
        raise CvRefused("That file is over the 5 MB limit.")
    if content.startswith(b"%PDF-"):
        return CvFileType.pdf
    _check_docx(content)
    return CvFileType.docx


def _check_docx(content: bytes) -> None:
    """Reads the zip's list of files and one small part. Nothing is unpacked to disk or parsed as XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > _MAX_UNPACKED_BYTES:
                raise CvRefused(_NOT_A_CV_FILE)
            names = {entry.filename.lower() for entry in entries}
            if any(name.endswith(_MACRO_PART) for name in names):
                raise CvRefused("Word files with macros aren't accepted. Save it as a plain .docx.")
            if _WORD_DOCUMENT not in names or _CONTENT_TYPES not in archive.namelist():
                raise CvRefused(_NOT_A_CV_FILE)
            if archive.getinfo(_CONTENT_TYPES).file_size > _MAX_CONTENT_TYPES_BYTES:
                raise CvRefused(_NOT_A_CV_FILE)
            types = archive.read(_CONTENT_TYPES)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        # Includes encrypted files and files that are damaged or only pretend to be a zip.
        raise CvRefused(_NOT_A_CV_FILE) from exc
    if _MACRO_MARKER in types.lower():
        raise CvRefused("Word files with macros aren't accepted. Save it as a plain .docx.")
    if _WORD_MAIN_TYPE not in types:
        raise CvRefused(_NOT_A_CV_FILE)


def clean_label(text: str) -> str:
    label = " ".join(text.split())
    if len(label) > MAX_ITEM_LENGTH:
        raise CvRefused(f"Keep each CV's job under {MAX_ITEM_LENGTH} characters.")
    return label


def cvs_of(session: Session, applicant_id: uuid.UUID) -> list[CvFile]:
    """Oldest first. Their bytes load only if `.content` is read."""
    return list(
        session.scalars(
            select(CvFile).where(CvFile.applicant_id == applicant_id).order_by(CvFile.uploaded_at, CvFile.id)
        )
    )


def owned_cv(session: Session, applicant_id: uuid.UUID, cv_id: uuid.UUID) -> CvFile | None:
    """None for an ID that isn't this person's, so nobody can reach another person's CV."""
    return session.scalar(select(CvFile).where(CvFile.id == cv_id, CvFile.applicant_id == applicant_id))


def _one_at_a_time(session: Session, applicant_id: uuid.UUID) -> None:
    """Holds the person's row until commit, so two requests can't both pass the cap or both set a primary."""
    session.execute(select(Applicant.id).where(Applicant.id == applicant_id).with_for_update())


def store_cv(
    session: Session, applicant_id: uuid.UUID, content: bytes | None, *, label: str = "", now: datetime | None = None
) -> CvFile:
    """Checks the file and adds it. A person's first CV is their primary."""
    file_type = check_cv(content)
    label = clean_label(label)
    _one_at_a_time(session, applicant_id)
    held = session.scalar(select(func.count()).select_from(CvFile).where(CvFile.applicant_id == applicant_id))
    if held >= MAX_CVS:
        raise CvRefused(f"You can keep up to {MAX_CVS} CVs. Delete one, or upload this as a new version of one.")
    row = CvFile(
        applicant_id=applicant_id,
        content=content,
        file_type=file_type,
        size_bytes=len(content),
        uploaded_at=now or datetime.now(UTC),
        label=label,
        is_primary=held == 0,
    )
    session.add(row)
    session.flush()
    return row


def replace_cv(
    session: Session,
    applicant_id: uuid.UUID,
    cv_id: uuid.UUID,
    content: bytes | None,
    *,
    label: str = "",
    now: datetime | None = None,
) -> CvFile:
    """A new version of one of the person's CVs. It keeps its label, unless a new one is given, and stays primary."""
    file_type = check_cv(content)
    label = clean_label(label)
    row = owned_cv(session, applicant_id, cv_id)
    if row is None:
        raise CvRefused("That CV isn't there any more. Upload this one as a new CV.")
    row.content = content
    row.file_type = file_type
    row.size_bytes = len(content)
    row.uploaded_at = now or datetime.now(UTC)
    row.label = label or row.label
    session.flush()
    return row


def delete_cv(session: Session, applicant_id: uuid.UUID, cv_id: uuid.UUID) -> None:
    """Deleting the primary makes the newest one left the primary."""
    _one_at_a_time(session, applicant_id)
    row = owned_cv(session, applicant_id, cv_id)
    if row is None:
        return
    session.delete(row)
    session.flush()
    if row.is_primary and (left := cvs_of(session, applicant_id)):
        left[-1].is_primary = True
        session.flush()


def save_cv_choices(
    session: Session, applicant_id: uuid.UUID, labels: Mapping[uuid.UUID, str], primary: uuid.UUID | None
) -> None:
    """Labels and the primary from the profile form. IDs that aren't this person's are ignored."""
    _one_at_a_time(session, applicant_id)
    rows = {row.id: row for row in cvs_of(session, applicant_id)}
    for cv_id, label in labels.items():
        if cv_id in rows:
            rows[cv_id].label = clean_label(label)
    if primary in rows and not rows[primary].is_primary:
        for row in rows.values():
            row.is_primary = False
        # The old primary is cleared first: the database allows only one at any moment.
        session.flush()
        rows[primary].is_primary = True
    session.flush()


class CvChoice(Protocol):
    label: str
    is_primary: bool
    uploaded_at: datetime


_SPELLINGS = (
    (re.compile(r"\bback[- ]end\b|\bserver[- ]side\b"), "backend"),
    (re.compile(r"\bfront[- ]end\b"), "frontend"),
    (re.compile(r"\bfull[- ]stack\b"), "fullstack"),
)
# Level words, closed-class words, and "data". "data" turns up in other jobs (patient data) and is not that job.
# shortcut: a one- or two-letter token is never a match (it, Go, C#), upgrade when a label is only that name.
_NOT_THE_JOB = frozenset(
    "graduate grad new junior jr entry level early career careers senior snr sr staff principal lead "
    "trainee programme program scheme role cv resume and of the for in to a an with at on by or as from "
    "into over than then but not nor it is you our be are was were been being we they them their your "
    "my he she his her its this that these those who what when where which how will can may must have "
    "has had about data".split()
)


def _job_words(text: str) -> set[str]:
    lowered = text.lower()
    for pattern, spelling in _SPELLINGS:
        lowered = pattern.sub(spelling, lowered)
    return {word for word in re.findall(r"[a-z0-9+#]+", lowered) if len(word) > 2} - _NOT_THE_JOB


def pick_base_cv[Cv: CvChoice](cvs: Sequence[Cv], *, title: str = "", advert: str = "") -> Cv | None:
    """The CV whose label best fits the job: most label words in its title, then the share of them in its
    title and in its advert. With no fit, or no job, the primary, then the newest."""
    in_title, in_advert = _job_words(title), _job_words(advert)

    def fit(cv: Cv) -> tuple[int, float, float, bool, datetime]:
        label = _job_words(cv.label)
        if not label:
            return 0, 0.0, 0.0, cv.is_primary, cv.uploaded_at
        hits = len(label & in_title)
        return hits, hits / len(label), len(label & in_advert) / len(label), cv.is_primary, cv.uploaded_at

    return max(cvs, key=fit, default=None)


def base_cv(session: Session, applicant_id: uuid.UUID, job: Job | None = None) -> CvFile | None:
    """The CV a job gets. Without a job, the primary."""
    if job is None:
        return pick_base_cv(cvs_of(session, applicant_id))
    return pick_base_cv(cvs_of(session, applicant_id), title=job.title, advert=job.description or "")


def cvs_by_applicant(session: Session, applicant_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[CvFile]]:
    grouped: dict[uuid.UUID, list[CvFile]] = defaultdict(list)
    for row in session.scalars(select(CvFile).where(CvFile.applicant_id.in_(set(applicant_ids)))):
        grouped[row.applicant_id].append(row)
    return grouped


def suggest_label(cv: CvFile, aimed_at: str) -> None:
    """An empty label takes the job the CV is written for. A label the person typed stays."""
    if not cv.label and aimed_at:
        cv.label = clean_label(aimed_at)


def application_cv(session: Session, applicant_id: uuid.UUID, application: Application | None) -> CvFile | None:
    """The CV chosen when the application was prepared, else the one its job gets now."""
    if application is not None and application.cv_file_id is not None:
        owned = owned_cv(session, applicant_id, application.cv_file_id)
        if owned is not None:
            return owned
    job = session.get(Job, application.job_id) if application is not None else None
    return base_cv(session, applicant_id, job)
