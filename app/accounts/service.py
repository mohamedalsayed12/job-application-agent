"""Register, log in, confirm an email and reset a password. Hashing comes from a library (D26).

Emailed links carry 32 random bytes. Only their SHA-256 is stored: the token is already
unguessable, so a slow hash would add nothing, and an exact-match lookup stays an index hit.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Applicant, AuthToken, AuthTokenPurpose, OnboardingStep

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 254
VERIFY_EMAIL_HOURS = 24
RESET_PASSWORD_MINUTES = 60
_LIFETIMES = {
    AuthTokenPurpose.verify_email: timedelta(hours=VERIFY_EMAIL_HOURS),
    AuthTokenPurpose.reset_password: timedelta(minutes=RESET_PASSWORD_MINUTES),
}

_hasher = PasswordHash.recommended()
# Verified when there is no password to check, so a missing account takes as long as a wrong password.
_UNUSABLE_HASH = _hasher.hash(secrets.token_urlsafe(32))


class AccountError(Exception):
    """A problem the person can read and act on."""


class InvalidLinkError(AccountError):
    """An emailed link that is unknown, expired or already used. One message for all three."""

    def __init__(self) -> None:
        super().__init__("This link has expired or has already been used.")


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


def check_registration(email: str, password: str) -> None:
    """The form checks alone, with no hashing and no database: raises AccountError on a mistyped form."""
    _clean_email(email)
    _check_password(password)


def register(session: Session, *, email: str, password: str) -> Applicant | None:
    """A new, unconfirmed account, or None when the email is taken.

    Never touches an existing row: claiming one by email alone would take over that account. A taken
    email still costs one hash, so the reply takes as long either way.
    """
    cleaned = _clean_email(email)
    _check_password(password)
    password_hash = _hasher.hash(password)
    if session.scalar(select(Applicant.id).where(Applicant.email == cleaned)) is not None:
        return None
    # A new account starts the sign-up wizard at the CV step, once its email is confirmed.
    applicant = Applicant(email=cleaned, password_hash=password_hash, onboarding_step=OnboardingStep.cv.value)
    try:
        with session.begin_nested():
            session.add(applicant)
            session.flush()
    except IntegrityError:
        # Someone registered the same email a moment ago.
        return None
    return applicant


def find_active(session: Session, email: str) -> Applicant | None:
    applicant = session.scalar(select(Applicant).where(Applicant.email == email.strip()))
    return applicant if applicant is not None and applicant.is_active else None


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


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _retire(session: Session, applicant: Applicant, purpose: AuthTokenPurpose) -> None:
    session.execute(
        update(AuthToken)
        .where(AuthToken.applicant_id == applicant.id, AuthToken.purpose == purpose.value, AuthToken.used_at.is_(None))
        .values(used_at=_now())
    )


def issue_token(session: Session, applicant: Applicant, purpose: AuthTokenPurpose) -> str:
    """The raw token goes into the email only. Any older link for the same purpose stops working."""
    _retire(session, applicant, purpose)
    token = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            applicant_id=applicant.id,
            purpose=purpose.value,
            token_hash=_digest(token),
            expires_at=_now() + _LIFETIMES[purpose],
        )
    )
    session.flush()
    return token


def _live_token(session: Session, token: str, purpose: AuthTokenPurpose, *, lock: bool) -> AuthToken:
    query = select(AuthToken).where(AuthToken.token_hash == _digest(token), AuthToken.purpose == purpose.value)
    if lock:
        # Two clicks at once: the second waits, then sees the link as used.
        query = query.with_for_update()
    row = session.scalar(query) if token else None
    if row is None or row.used_at is not None or row.expires_at <= _now():
        raise InvalidLinkError
    return row


def _owner(session: Session, row: AuthToken) -> Applicant:
    applicant = session.get(Applicant, row.applicant_id)
    if applicant is None or not applicant.is_active:
        raise InvalidLinkError
    return applicant


def check_token(session: Session, token: str, purpose: AuthTokenPurpose) -> Applicant:
    """Whether a link still works, without using it up. For showing the form a link opens."""
    return _owner(session, _live_token(session, token, purpose, lock=False))


def confirm_email(session: Session, token: str) -> Applicant:
    row = _live_token(session, token, AuthTokenPurpose.verify_email, lock=True)
    applicant = _owner(session, row)
    row.used_at = _now()
    if applicant.email_verified_at is None:
        applicant.email_verified_at = _now()
    session.flush()
    return applicant


def reset_password(session: Session, *, token: str, password: str) -> Applicant:
    """Sets the new password and logs out every session, including any an attacker holds.

    The password is checked first, so a too-short one leaves the link working for another try.
    """
    _check_password(password)
    row = _live_token(session, token, AuthTokenPurpose.reset_password, lock=True)
    applicant = _owner(session, row)
    applicant.password_hash = _hasher.hash(password)
    # Opening the emailed link proves the person controls the address.
    if applicant.email_verified_at is None:
        applicant.email_verified_at = _now()
    end_all_sessions(applicant)
    _retire(session, applicant, AuthTokenPurpose.reset_password)
    session.flush()
    return applicant
