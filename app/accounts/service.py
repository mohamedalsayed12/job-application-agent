"""Register, log in and set a password. Hashing and signing come from libraries (D26)."""

from __future__ import annotations

import secrets
import uuid

from itsdangerous import BadSignature, URLSafeTimedSerializer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Applicant

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 254
SET_PASSWORD_LINK_SECONDS = 24 * 60 * 60

_hasher = PasswordHash.recommended()
# Verified when there is no password to check, so a missing account takes as long as a wrong password.
_UNUSABLE_HASH = _hasher.hash(secrets.token_urlsafe(32))


class AccountError(Exception):
    """A problem the person can read and act on."""


class InvalidLinkError(AccountError):
    """A set-password link that is altered, expired or already used."""


def _clean_email(email: str) -> str:
    cleaned = email.strip()
    local, at, domain = cleaned.partition("@")
    if not (local and at and domain) or len(cleaned) > MAX_EMAIL_LENGTH or any(c.isspace() for c in cleaned):
        raise AccountError("Enter a valid email address.")
    return cleaned


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"Use a password of at most {MAX_PASSWORD_LENGTH} characters.")


def register(session: Session, *, email: str, password: str) -> Applicant:
    """Never touches an existing row: claiming one by email alone would take over that account."""
    cleaned = _clean_email(email)
    _check_password(password)
    applicant = Applicant(email=cleaned, password_hash=_hasher.hash(password))
    session.add(applicant)
    try:
        session.flush()
    except IntegrityError as exc:
        raise AccountError("This email can't be registered. If it's yours, log in instead.") from exc
    return applicant


def authenticate(session: Session, *, email: str, password: str) -> Applicant | None:
    applicant = session.scalar(select(Applicant).where(Applicant.email == email.strip()))
    if len(password) > MAX_PASSWORD_LENGTH:
        return None
    stored = applicant.password_hash if applicant else None
    matched, upgraded = _hasher.verify_and_update(password, stored or _UNUSABLE_HASH)
    if applicant is None or stored is None or not matched or not applicant.is_active:
        return None
    if upgraded:
        applicant.password_hash = upgraded
    return applicant


def end_all_sessions(applicant: Applicant) -> None:
    applicant.session_version += 1


def delete_account(session: Session, applicant: Applicant) -> None:
    """The database removes the profile, CV, preferences and matches with this row, and keeps search history."""
    session.delete(applicant)
    session.flush()


def _link_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="set-password")


def set_password_token(applicant: Applicant) -> str:
    return _link_signer().dumps([str(applicant.id), applicant.session_version])


def set_password(session: Session, *, token: str, password: str, max_age: int = SET_PASSWORD_LINK_SECONDS) -> Applicant:
    """Using the link raises the session version, which also makes the link stop working."""
    applicant = applicant_for_token(session, token, max_age=max_age)
    _check_password(password)
    applicant.password_hash = _hasher.hash(password)
    end_all_sessions(applicant)
    session.flush()
    return applicant


def applicant_for_token(session: Session, token: str, *, max_age: int = SET_PASSWORD_LINK_SECONDS) -> Applicant:
    try:
        applicant_id, version = _link_signer().loads(token, max_age=max_age)
    except BadSignature as exc:
        raise InvalidLinkError("This link has expired or has already been used.") from exc
    applicant = session.get(Applicant, uuid.UUID(applicant_id))
    if applicant is None or not applicant.is_active or applicant.session_version != version:
        raise InvalidLinkError("This link has expired or has already been used.")
    return applicant
