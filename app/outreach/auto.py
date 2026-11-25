"""After "Submitted": find people and draft outreach without being asked (D76).

Marking an application submitted queues it. Each worker cycle takes a few queued applications, finds the
hiring manager and people doing the same job, finds an email for each, researches them and drafts a Gmail
message and a LinkedIn note. Nothing is sent: the person reads every draft and sends it with one click.
A spending cap pauses the work and the next cycle carries on where it stopped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import (
    Application,
    ApplicationStatus,
    Job,
    OutreachChannel,
    OutreachContact,
    OutreachDraft,
    OutreachRoleKind,
)
from app.outreach.draft import draft_for_contact
from app.outreach.email_find import find_email
from app.outreach.find import find_contacts
from app.outreach.research import research_contact

QUEUED = "queued"
READY = "ready"
NO_CONTACTS = "no_contacts"
_STOP_WORDS = {"and", "or", "of", "the", "a", "an", "to", "in", "for", "with", "junior", "graduate", "senior", "entry", "level", "i", "ii"}


@dataclass(slots=True)
class AutoReport:
    ready: int = 0
    drafts: int = 0
    stopped_by: str | None = None
    problems: list[str] = field(default_factory=list)


def queue_outreach(application: Application) -> None:
    """Called when the person marks the application submitted; a queue already worked is left alone."""
    if application.outreach_state is None:
        application.outreach_state = QUEUED


def pick_contacts(contacts: list[OutreachContact], job_title: str, limit: int) -> list[OutreachContact]:
    """The hiring manager first, then people doing this job, then recruiters, then everyone else."""
    wanted = _title_words(job_title)

    def rank(contact: OutreachContact) -> int:
        same_job = bool(wanted & _title_words(contact.title or ""))
        if contact.role_kind == OutreachRoleKind.hiring_manager.value:
            return 0
        if same_job:
            return 1
        if contact.role_kind == OutreachRoleKind.recruiter.value:
            return 2
        if contact.role_kind == OutreachRoleKind.employee.value:
            return 3
        return 4

    return sorted(contacts, key=lambda c: (rank(c), c.created_at or datetime.min.replace(tzinfo=UTC)))[:limit]


async def prepare_outreach(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None = None,
) -> AutoReport:
    report = AutoReport()
    if not settings.apify_api_token.strip():
        report.stopped_by = "not_configured"
        return report
    queued = list(
        session.scalars(
            select(Application)
            .where(Application.outreach_state == QUEUED, Application.status == ApplicationStatus.submitted.value)
            .order_by(Application.updated_at)
            .limit(settings.outreach_auto_per_cycle)
        )
    )
    for application in queued:
        stopped = await _prepare_one(session, application, gateway=gateway, settings=settings, persist=persist, now=now, report=report)
        if stopped:
            report.stopped_by = stopped
            break
    return report


async def _prepare_one(
    session: Session,
    application: Application,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None,
    report: AutoReport,
) -> str | None:
    """None when this application is finished; otherwise why work stopped (it stays queued)."""
    job = session.get(Job, application.job_id)
    if job is None:
        application.outreach_state = NO_CONTACTS
        persist()
        return None
    contacts = _contacts(session, application)
    # People are looked up once per application; a later cycle only finishes the drafts.
    if not contacts:
        found = await find_contacts(
            session, applicant_id=application.applicant_id, job=job, gateway=gateway, settings=settings, persist=persist, now=now
        )
        if found.stopped_by:
            return found.stopped_by
        contacts = _contacts(session, application)
        if not contacts:
            application.outreach_state = NO_CONTACTS
            if found.problem:
                report.problems.append(found.problem)
            persist()
            return None
    for contact in pick_contacts(contacts, job.title, settings.outreach_auto_contacts):
        email = await find_email(session, contact, job, gateway=gateway, settings=settings, persist=persist, now=now)
        researched = await research_contact(session, contact, gateway=gateway, settings=settings, persist=persist, now=now)
        if researched.stopped_by == "budget":
            return "budget"
        channels = [OutreachChannel.linkedin_note] if contact.linkedin_url else []
        if email.email:
            channels.insert(0, OutreachChannel.gmail)
        for channel in channels:
            if _has_draft(session, contact, channel):
                continue
            drafted = await draft_for_contact(
                session,
                applicant_id=application.applicant_id,
                job=job,
                contact=contact,
                research=researched.research,
                channel=channel,
                gateway=gateway,
                settings=settings,
                persist=persist,
                now=now,
            )
            if drafted.stopped_by:
                return drafted.stopped_by
            if drafted.draft is not None:
                report.drafts += 1
    application.outreach_state = READY
    report.ready += 1
    persist()
    return None


def drafts_waiting(session: Session, application: Application) -> int:
    """Drafts for this application's contacts that the person has not sent or copied yet."""
    contact_ids = [c.id for c in _contacts(session, application)]
    if not contact_ids:
        return 0
    return len(
        session.scalars(
            select(OutreachDraft.id).where(
                OutreachDraft.contact_id.in_(contact_ids), OutreachDraft.status.in_(("draft", "edited"))
            )
        ).all()
    )


def _contacts(session: Session, application: Application) -> list[OutreachContact]:
    return list(
        session.scalars(
            select(OutreachContact).where(
                OutreachContact.applicant_id == application.applicant_id, OutreachContact.job_id == application.job_id
            )
        )
    )


def _has_draft(session: Session, contact: OutreachContact, channel: OutreachChannel) -> bool:
    return (
        session.scalar(
            select(OutreachDraft.id).where(OutreachDraft.contact_id == contact.id, OutreachDraft.channel == channel.value).limit(1)
        )
        is not None
    )


def _title_words(title: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]+", title.lower()) if word not in _STOP_WORDS and len(word) > 2}
