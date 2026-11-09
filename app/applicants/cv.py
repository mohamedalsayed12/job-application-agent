"""Check, store, replace and delete a person's CV. Its text is never read (D25, D38)."""

from __future__ import annotations

import io
import uuid
import zipfile
import zlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CvFile, CvFileType

MAX_CV_BYTES = 5 * 1024 * 1024
# Room for the form's token and part headers around a file of the largest size.
MAX_UPLOAD_BODY_BYTES = MAX_CV_BYTES + 16 * 1024
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


def cv_for(session: Session, applicant_id: uuid.UUID) -> CvFile | None:
    """The file's details. Its bytes load only if `.content` is read."""
    return session.scalar(select(CvFile).where(CvFile.applicant_id == applicant_id))


def store_cv(session: Session, applicant_id: uuid.UUID, content: bytes | None, *, now: datetime | None = None) -> CvFile:
    """Checks the file, then replaces any earlier one."""
    file_type = check_cv(content)
    row = cv_for(session, applicant_id)
    if row is None:
        row = CvFile(applicant_id=applicant_id)
        session.add(row)
    row.content = content
    row.file_type = file_type
    row.size_bytes = len(content)
    row.uploaded_at = now or datetime.now(UTC)
    session.flush()
    return row


def delete_cv(session: Session, applicant_id: uuid.UUID) -> None:
    row = cv_for(session, applicant_id)
    if row is not None:
        session.delete(row)
        session.flush()
