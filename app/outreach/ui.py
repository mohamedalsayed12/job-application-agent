"""Outreach under each job summary: the drafted messages, or the next step toward them (NEW_FEATURES section 4).

Everything here reuses the outreach page's routes. The only new thing is reading the data for a whole
page of jobs at once.
"""

from __future__ import annotations

import html
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.ui import applied_button
from app.models import (
    Application,
    ApplicationStatus,
    OutreachChannel,
    OutreachContact,
    OutreachDraft,
)
from app.outreach.draft import current_body


@dataclass(frozen=True, slots=True)
class ContactMessages:
    contact: OutreachContact
    gmail: OutreachDraft | None = None
    note: OutreachDraft | None = None


@dataclass(frozen=True, slots=True)
class JobOutreach:
    applied: bool = False
    contacts: list[ContactMessages] = field(default_factory=list)


def load_outreach(session: Session, applicant_id: uuid.UUID, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, JobOutreach]:
    """Outreach state for every job in `job_ids`, in three queries however many jobs there are."""
    if not job_ids:
        return {}
    applied = set(
        session.scalars(
            select(Application.job_id).where(
                Application.applicant_id == applicant_id,
                Application.job_id.in_(job_ids),
                Application.status == ApplicationStatus.submitted.value,
            )
        )
    )
    contacts = session.scalars(
        select(OutreachContact)
        .where(OutreachContact.applicant_id == applicant_id, OutreachContact.job_id.in_(job_ids))
        .order_by(OutreachContact.created_at.desc())
    ).all()
    latest: dict[tuple[uuid.UUID, str], OutreachDraft] = {}
    for draft in session.scalars(
        select(OutreachDraft)
        .where(OutreachDraft.applicant_id == applicant_id, OutreachDraft.job_id.in_(job_ids))
        .order_by(OutreachDraft.created_at)
    ):
        latest[(draft.contact_id, draft.channel)] = draft
    by_job: dict[uuid.UUID, list[ContactMessages]] = {job_id: [] for job_id in job_ids}
    for contact in contacts:
        by_job[contact.job_id].append(
            ContactMessages(
                contact,
                gmail=latest.get((contact.id, OutreachChannel.gmail.value)),
                note=latest.get((contact.id, OutreachChannel.linkedin_note.value)),
            )
        )
    return {job_id: JobOutreach(applied=job_id in applied, contacts=by_job[job_id]) for job_id in job_ids}


def _message(label: str, draft: OutreachDraft, *, subject: bool) -> str:
    head = f"<b>Subject:</b> {html.escape(draft.subject)}<br/>" if subject and draft.subject else ""
    return (
        f'<details class="outreach-message"><summary>{html.escape(label)}</summary>'
        f'<p style="white-space: pre-line">{head}{html.escape(current_body(draft))}</p></details>'
    )


def _contact(job_id: uuid.UUID, item: ContactMessages, csrf: str) -> str:
    contact = item.contact
    jid = html.escape(str(job_id))
    page = f"/jobs/{jid}/outreach/{html.escape(str(contact.id))}"
    who = f"<b>{html.escape(contact.full_name)}</b> · {html.escape(contact.title or 'Title unknown')}"
    if item.gmail is None and item.note is None:
        return (
            f'<div class="outreach-contact"><p>{who}</p>'
            f'<form method="post" action="{page}/draft" class="inline">{csrf}'
            '<button type="submit">Research and draft</button></form></div>'
        )
    parts = [f"<p>{who}</p>"]
    if item.gmail is not None:
        parts.append(_message("Gmail message", item.gmail, subject=True))
    actions = [f'<a href="{page}">Edit or send</a>']
    if item.note is not None:
        parts.append(_message("LinkedIn note", item.note, subject=False))
        actions.append(
            f'<form method="post" action="{page}/copy" class="inline">{csrf}'
            f'<input type="hidden" name="draft_id" value="{html.escape(str(item.note.id))}"/>'
            '<button type="submit">Copy LinkedIn note</button></form>'
        )
    parts.append(f'<p>{" · ".join(actions)}</p>')
    return f'<div class="outreach-contact">{"".join(parts)}</div>'


def outreach_block(job_id: uuid.UUID, outreach: JobOutreach, csrf: str) -> str:
    jid = html.escape(str(job_id))
    if not outreach.applied:
        return (
            '<div class="outreach"><p class="muted">Outreach: mark this job as applied to reach out to people '
            f"at the company.</p><p>{applied_button(csrf, job_id)}</p></div>"
        )
    if not outreach.contacts:
        return (
            '<div class="outreach"><p class="muted">Outreach: no contacts yet.</p>'
            f'<form method="post" action="/jobs/{jid}/outreach/find" class="inline">{csrf}'
            '<button type="submit">Find people</button></form></div>'
        )
    rows = "".join(_contact(job_id, item, csrf) for item in outreach.contacts)
    return (
        f'<div class="outreach"><p><b>Outreach</b> · <a href="/jobs/{jid}/outreach">All contacts</a></p>{rows}</div>'
    )
