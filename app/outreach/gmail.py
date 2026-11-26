"""Gmail OAuth connect and send-through-gateway (no SMTP app passwords)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest
from app.models import (
    GmailConnection,
    OutreachChannel,
    OutreachDraft,
    OutreachDraftStatus,
    OutreachSend,
    OutreachSendStatus,
)
from app.outreach.draft import current_body
from app.outreach.seal import seal, unseal

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = "https://www.googleapis.com/auth/gmail.send"


@dataclass(frozen=True, slots=True)
class SendResult:
    status: str
    message_id: str | None = None
    error: str | None = None


def gmail_configured(settings: Settings) -> bool:
    return bool(settings.gmail_client_id.strip() and settings.gmail_client_secret.strip())


def active_connection(session: Session, applicant_id: UUID) -> GmailConnection | None:
    row = session.scalar(select(GmailConnection).where(GmailConnection.applicant_id == applicant_id))
    if row is None or row.revoked_at is not None:
        return None
    return row


def authorize_url(settings: Settings, *, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.gmail_client_id,
            "redirect_uri": settings.gmail_redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


async def exchange_code(
    gateway: FetchGateway,
    settings: Settings,
    *,
    code: str,
) -> dict[str, Any] | str:
    body = urlencode(
        {
            "code": code,
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri": settings.gmail_redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    outcome = await gateway.fetch(
        FetchRequest(
            url=TOKEN_URL,
            method="POST",
            kind="api",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
    )
    if isinstance(outcome, FetchRefusal):
        return outcome.reason
    if outcome.status_code >= 400:
        return f"token_http_{outcome.status_code}"
    try:
        data = json.loads(outcome.body)
    except ValueError:
        return "token_not_json"
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return "missing_refresh_token"
    return data


async def profile_email(gateway: FetchGateway, access_token: str) -> str | None:
    outcome = await gateway.fetch(
        FetchRequest(
            url="https://gmail.googleapis.com/gmail/v1/users/me/profile",
            method="GET",
            kind="api",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    )
    if isinstance(outcome, FetchRefusal) or outcome.status_code >= 400:
        return None
    try:
        data = json.loads(outcome.body)
    except ValueError:
        return None
    email = data.get("emailAddress") if isinstance(data, dict) else None
    return str(email).strip() if email else None


def save_connection(
    session: Session,
    *,
    applicant_id: UUID,
    email: str,
    refresh_token: str,
    settings: Settings,
    now: datetime | None = None,
) -> GmailConnection:
    at = now or datetime.now(UTC)
    sealed = seal(settings.gmail_seal_secret(), refresh_token)
    row = session.scalar(select(GmailConnection).where(GmailConnection.applicant_id == applicant_id))
    if row is None:
        row = GmailConnection(
            applicant_id=applicant_id,
            email=email,
            refresh_token_sealed=sealed,
            scopes=SCOPES,
            connected_at=at,
            revoked_at=None,
        )
        session.add(row)
    else:
        row.email = email
        row.refresh_token_sealed = sealed
        row.scopes = SCOPES
        row.connected_at = at
        row.revoked_at = None
    session.flush()
    return row


def revoke_connection(session: Session, applicant_id: UUID, *, now: datetime | None = None) -> None:
    row = session.scalar(select(GmailConnection).where(GmailConnection.applicant_id == applicant_id))
    if row is None:
        return
    row.revoked_at = now or datetime.now(UTC)
    session.flush()


def sends_today(session: Session, applicant_id: UUID, *, now: datetime) -> int:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.scalar(
            select(func.count())
            .select_from(OutreachSend)
            .join(OutreachDraft, OutreachDraft.id == OutreachSend.draft_id)
            .where(
                OutreachDraft.applicant_id == applicant_id,
                OutreachSend.channel == OutreachChannel.gmail.value,
                OutreachSend.status.in_((OutreachSendStatus.sent.value, OutreachSendStatus.dry_run.value)),
                OutreachSend.created_at >= start,
            )
        )
        or 0
    )


def already_sent(session: Session, draft: OutreachDraft) -> bool:
    """True if this applicant already mailed this contact for this job (any draft)."""
    return (
        session.scalar(
            select(OutreachSend.id)
            .join(OutreachDraft, OutreachDraft.id == OutreachSend.draft_id)
            .where(
                OutreachDraft.applicant_id == draft.applicant_id,
                OutreachDraft.contact_id == draft.contact_id,
                OutreachDraft.job_id == draft.job_id,
                OutreachSend.channel == OutreachChannel.gmail.value,
                OutreachSend.status.in_((OutreachSendStatus.sent.value, OutreachSendStatus.dry_run.value)),
            )
        )
        is not None
    )


async def send_draft(
    session: Session,
    draft: OutreachDraft,
    *,
    to_email: str,
    gateway: FetchGateway,
    settings: Settings,
    confirm: bool,
    now: datetime | None = None,
) -> SendResult:
    at = now or datetime.now(UTC)
    if not confirm:
        return SendResult(status="failed", error="confirmation_required")
    if draft.channel != OutreachChannel.gmail.value:
        return SendResult(status="failed", error="wrong_channel")
    from app.outreach.normalize import parse_email

    cleaned = parse_email(to_email)
    if cleaned is None:
        return SendResult(status="failed", error="no_recipient_email")
    to_email = cleaned
    # Lock the draft so two tabs cannot both pass the already-sent check.
    locked = session.get(OutreachDraft, draft.id, with_for_update=True)
    if locked is None:
        return SendResult(status="failed", error="missing_draft")
    draft = locked
    if already_sent(session, draft):
        return SendResult(status="failed", error="already_sent")
    if sends_today(session, draft.applicant_id, now=at) >= settings.outreach_daily_send_cap:
        return SendResult(status="failed", error="daily_cap")

    body = current_body(draft)
    subject = (draft.subject or f"About {draft.job_id}").strip()
    connection = active_connection(session, draft.applicant_id)
    if connection is None:
        return SendResult(status="failed", error="gmail_not_connected")

    if settings.outreach_gmail_dry_run:
        row = OutreachSend(
            draft_id=draft.id,
            channel=OutreachChannel.gmail.value,
            provider_message_id=None,
            status=OutreachSendStatus.dry_run.value,
            error=None,
        )
        session.add(row)
        draft.status = OutreachDraftStatus.sent.value
        session.flush()
        return SendResult(status=OutreachSendStatus.dry_run.value)

    try:
        refresh = unseal(settings.gmail_seal_secret(), connection.refresh_token_sealed)
    except ValueError:
        return SendResult(status="failed", error="token_unreadable")

    token = await _access_token(gateway, settings, refresh)
    if token.error or not token.access_token:
        err = token.error or "missing_access_token"
        row = OutreachSend(
            draft_id=draft.id,
            channel=OutreachChannel.gmail.value,
            status=OutreachSendStatus.failed.value,
            error=err,
        )
        session.add(row)
        session.flush()
        return SendResult(status="failed", error=err)

    raw = _mime(to=to_email, subject=subject, body=body, from_email=connection.email)
    outcome = await gateway.fetch(
        FetchRequest(
            url=GMAIL_SEND_URL,
            method="POST",
            kind="api",
            headers={"Authorization": f"Bearer {token.access_token}", "Content-Type": "application/json"},
            body=json.dumps({"raw": raw}).encode(),
        )
    )
    if isinstance(outcome, FetchRefusal) or outcome.status_code >= 400:
        err = outcome.reason if isinstance(outcome, FetchRefusal) else f"gmail_http_{outcome.status_code}"
        row = OutreachSend(
            draft_id=draft.id,
            channel=OutreachChannel.gmail.value,
            status=OutreachSendStatus.failed.value,
            error=err,
        )
        session.add(row)
        session.flush()
        return SendResult(status="failed", error=err)

    message_id = None
    try:
        data = json.loads(outcome.body)
        if isinstance(data, dict):
            message_id = str(data.get("id") or "") or None
    except ValueError:
        message_id = None
    row = OutreachSend(
        draft_id=draft.id,
        channel=OutreachChannel.gmail.value,
        provider_message_id=message_id,
        status=OutreachSendStatus.sent.value,
    )
    session.add(row)
    draft.status = OutreachDraftStatus.sent.value
    session.flush()
    return SendResult(status=OutreachSendStatus.sent.value, message_id=message_id)


def record_copy(session: Session, draft: OutreachDraft) -> OutreachSend:
    """The person took the message to send it themselves (opened in Gmail or copied)."""
    row = OutreachSend(
        draft_id=draft.id,
        channel=draft.channel,
        status=OutreachSendStatus.copied.value,
    )
    session.add(row)
    draft.status = OutreachDraftStatus.copied.value
    session.flush()
    return row


def _mime(*, to: str, subject: str, body: str, from_email: str) -> str:
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["from"] = from_email
    message["subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


@dataclass(frozen=True, slots=True)
class AccessToken:
    access_token: str | None = None
    error: str | None = None


async def _access_token(gateway: FetchGateway, settings: Settings, refresh_token: str) -> AccessToken:
    body = urlencode(
        {
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    outcome = await gateway.fetch(
        FetchRequest(
            url=TOKEN_URL,
            method="POST",
            kind="api",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
    )
    if isinstance(outcome, FetchRefusal):
        return AccessToken(error=outcome.reason)
    if outcome.status_code >= 400:
        return AccessToken(error=f"refresh_http_{outcome.status_code}")
    try:
        data = json.loads(outcome.body)
    except ValueError:
        return AccessToken(error="refresh_not_json")
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        return AccessToken(error="missing_access_token")
    return AccessToken(access_token=str(token))
