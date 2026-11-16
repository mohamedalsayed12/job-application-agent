"""Issue and verify hashed tokens for the fill browser extension."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExtensionDevice

TOKEN_BYTES = 32


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(session: Session, applicant_id: uuid.UUID) -> str:
    """Rotate the device token and return the plaintext once."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = session.scalar(select(ExtensionDevice).where(ExtensionDevice.applicant_id == applicant_id))
    if row is None:
        row = ExtensionDevice(applicant_id=applicant_id, token_hash=_hash(token))
        session.add(row)
    else:
        row.token_hash = _hash(token)
        row.rotated_at = datetime.now(UTC)
    session.flush()
    return token


def applicant_for_token(session: Session, token: str) -> uuid.UUID | None:
    if not token or len(token) > 200:
        return None
    digest = _hash(token)
    row = session.scalar(select(ExtensionDevice).where(ExtensionDevice.token_hash == digest))
    if row is None:
        # Constant-time compare against a dummy so missing tokens aren't faster.
        hmac.compare_digest(digest, _hash("missing"))
        return None
    if not hmac.compare_digest(row.token_hash, digest):
        return None
    return row.applicant_id
