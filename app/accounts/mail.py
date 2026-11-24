"""Account emails: confirm an address, reset a password, or say an address already has an account.

MAIL_BACKEND picks where they go. "smtp" sends through SMTP_*; "resend" posts to Resend's HTTPS API
(using SMTP_PASSWORD as the API key — needed on hosts that block outbound SMTP); "memory" keeps them
in ``outbox`` for tests and never sends. Nothing here logs a message body, because bodies hold
one-time links.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str


# The test double: every message the "memory" backend would have sent, oldest first.
outbox: list[Mail] = []


def link(path: str, token: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}{path}?{urlencode({'token': token})}"


def _send_smtp(settings: Settings, mail: Mail) -> None:
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message.set_content(mail.text)
    timeout = settings.smtp_timeout_seconds
    if settings.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def _send_resend(settings: Settings, mail: Mail) -> None:
    """HTTPS to api.resend.com — works where cloud hosts block SMTP ports."""
    key = settings.smtp_password.strip()
    if not key:
        raise OSError("SMTP_PASSWORD must hold the Resend API key when MAIL_BACKEND=resend")
    with httpx.Client(timeout=settings.smtp_timeout_seconds) as client:
        response = client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "from": settings.mail_from,
                "to": [mail.to],
                "subject": mail.subject,
                "text": mail.text,
            },
        )
        response.raise_for_status()


def send(mail: Mail) -> None:
    """Runs after the response is sent, so how long sending takes says nothing about whether an account exists."""
    settings = get_settings()
    if settings.mail_backend == "memory":
        outbox.append(mail)
        return
    try:
        if settings.mail_backend == "resend":
            _send_resend(settings, mail)
        else:
            _send_smtp(settings, mail)
    except (OSError, smtplib.SMTPException, httpx.HTTPError) as exc:
        # The kind of failure only: the message holds a one-time link, and the error may quote it.
        logger.error("account email not sent (%s): %s", mail.subject, type(exc).__name__)


def verify_email(to: str, token: str, hours: int) -> Mail:
    return Mail(
        to=to,
        subject="Confirm your email for Job Agent",
        text=(
            "Confirm your email address to start using Job Agent:\n\n"
            f"{link('/verify-email/confirm', token)}\n\n"
            f"The link works once and expires in {hours} hours. If you didn't create an account, ignore this email."
        ),
    )


def reset_password(to: str, token: str, minutes: int) -> Mail:
    return Mail(
        to=to,
        subject="Reset your Job Agent password",
        text=(
            "Someone asked to reset the password for this Job Agent account. To choose a new password, open:\n\n"
            f"{link('/reset-password', token)}\n\n"
            f"The link works once and expires in {minutes} minutes. Resetting your password logs you out everywhere.\n"
            "If you didn't ask for this, ignore this email; your password hasn't changed."
        ),
    )


def already_registered(to: str) -> Mail:
    base = get_settings().public_base_url.rstrip("/")
    return Mail(
        to=to,
        subject="You already have a Job Agent account",
        text=(
            "Someone tried to create a Job Agent account with this email address, which already has one.\n\n"
            f"If it was you, log in at {base}/login, or reset your password at {base}/forgot-password.\n"
            "If it wasn't you, you can ignore this email; nothing has changed."
        ),
    )
