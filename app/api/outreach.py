"""Outreach UI: find contacts, draft an email to each, and open it in the person's own Gmail to send."""

from __future__ import annotations

import html
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.api.forms import csrf_field, require_csrf
from app.applications.prepare import has_applied
from app.applications.ui import applied_button
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.models import (
    Applicant,
    ApplicantJobMatch,
    Job,
    JobFreshness,
    OutreachChannel,
    OutreachContact,
    OutreachDraft,
    OutreachDraftStatus,
    OutreachResearch,
)
from app.outreach.draft import current_body, draft_for_contact
from app.outreach.email_find import email_note, find_email
from app.outreach.find import find_contacts
from app.outreach.gmail import active_connection, record_copy, revoke_connection
from app.outreach.patterns import load_patterns
from app.outreach.research import research_contact

router = APIRouter(tags=["outreach"])
_CSRF = [Depends(require_csrf)]
_SEE_OTHER = 303
_APPLY_FIRST = "Mark this job as applied first. Outreach comes after you've sent your application."
async def get_gateway() -> AsyncIterator[FetchGateway]:
    gateway = FetchGateway()
    try:
        yield gateway
    finally:
        await gateway.aclose()


def _open_matched_job(db: Session, applicant: Applicant, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.freshness != JobFreshness.open:
        raise HTTPException(status_code=404)
    match = db.scalar(
        select(ApplicantJobMatch).where(
            ApplicantJobMatch.applicant_id == applicant.id,
            ApplicantJobMatch.job_id == job.id,
        )
    )
    if match is None:
        raise HTTPException(status_code=404)
    return job


def _own_contact(db: Session, applicant: Applicant, contact_id: uuid.UUID) -> OutreachContact:
    contact = db.get(OutreachContact, contact_id)
    if contact is None or contact.applicant_id != applicant.id:
        raise HTTPException(status_code=404)
    return contact


def _apply_first(job: Job) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job.id}/outreach?notice={quote(_APPLY_FIRST)}", status_code=_SEE_OTHER)


# What the codes in ?notice= mean to the person: (sentence, is it a problem).
_NOTICES: dict[str, tuple[str, bool]] = {
    "not_configured": ("Finding people and drafting messages isn't set up on this server yet.", True),
    "budget": ("The monthly spending cap is used up, so finding people is paused until next month.", True),
    "hm_actor_empty": ("We couldn't find the hiring manager for this job. Try again later.", True),
    "employees_actor_empty": (
        "No people were found at this company right now. Try again later, or look them up on LinkedIn yourself.", True
    ),
    "draft_failed": ("Could not draft a message for this person. Try again, or write it yourself.", True),
    "saved": ("Draft saved.", False),
    "disconnected": ("Gmail is disconnected.", False),
}


def _error(message: str | None) -> str:
    """A notice code as a sentence. Unknown text shows as it is, as a problem unless it reports contacts found."""
    if not message:
        return ""
    text, problem = _NOTICES.get(message) or (message, not message.startswith("Found "))
    return f'<p class="{"error" if problem else "notice"}">{html.escape(text)}</p>'


def _find_button(job: Job, request: Request, *, applied: bool) -> str:
    if not applied:
        return f"<p>{html.escape(_APPLY_FIRST)}</p><p>{applied_button(csrf_field(request), job.id)}</p>"
    return f"""<form method="post" action="/jobs/{job.id}/outreach/find">
  {csrf_field(request)}
  <button type="submit" class="primary">Find people</button>
</form>"""


def _contacts_html(
    job: Job, contacts: list[OutreachContact], request: Request, *, applied: bool, notice: str | None = None
) -> str:
    rows = []
    for contact in contacts:
        title = html.escape(contact.title or "Title unknown")
        name = html.escape(contact.full_name)
        kind = html.escape(contact.role_kind.replace("_", " "))
        # email_note already says when there is no address, so the address is shown only when there is one.
        email = f"{html.escape(contact.email) + '. ' if contact.email else ''}{html.escape(email_note(contact))}"
        # People are found by company name, so another firm with the same name can slip in: show whose staff they are.
        employer = html.escape(contact.company_name or job.company_name)
        rows.append(
            f"""
<div class="card">
  <p><b>{name}</b> · {title} · {kind}</p>
  <p class="muted">Works at: {employer}. Check their LinkedIn shows the employer behind this job before you write.</p>
  <p class="muted">{email}</p>
  <form method="post" action="/jobs/{job.id}/outreach/{contact.id}/draft">
    {csrf_field(request)}
    <button type="submit" class="primary">Research and draft an email</button>
  </form>
</div>"""
        )
    body = "\n".join(rows) if rows else '<p class="muted">No contacts stored yet.</p>'
    return f"""
<p><a href="/jobs/{job.id}">← Back to job</a></p>
<div class="card">
<h1>Outreach · {html.escape(job.title)}</h1>
<p class="muted">{html.escape(job.company_name)}. We find people who work there from public LinkedIn profiles
and draft an email to each. You read it, open it in your own Gmail and send it yourself.
You are responsible for following UK rules on unsolicited email (PECR).</p>
{_error(notice)}
{_find_button(job, request, applied=applied)}
</div>
{body}
"""


@router.get("/jobs/{job_id}/outreach", response_class=HTMLResponse)
def outreach_list(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = _open_matched_job(db, applicant, job_id)
    contacts = list(
        db.scalars(
            select(OutreachContact)
            .where(OutreachContact.applicant_id == applicant.id, OutreachContact.job_id == job.id)
            .order_by(OutreachContact.created_at.desc())
        )
    )
    notice = request.query_params.get("notice")
    return page(
        f"Outreach · {job.title}",
        _contacts_html(job, contacts, request, applied=has_applied(db, applicant.id, job.id), notice=notice),
        header=applicant_bar(request, "matches"),
        narrow=True,
    )


@router.post("/jobs/{job_id}/outreach/find", response_class=HTMLResponse, dependencies=_CSRF)
async def outreach_find(
    request: Request,
    job_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    job = _open_matched_job(db, applicant, job_id)
    if not has_applied(db, applicant.id, job.id):
        return _apply_first(job)
    report = await find_contacts(
        db,
        applicant_id=applicant.id,
        job=job,
        gateway=gateway,
        settings=settings,
        persist=db.commit,
    )
    notice = report.stopped_by or report.problem or f"Found {report.added} new {'contact' if report.added == 1 else 'contacts'}."
    return RedirectResponse(f"/jobs/{job.id}/outreach?notice={quote(notice)}", status_code=_SEE_OTHER)


@router.post("/jobs/{job_id}/outreach/{contact_id}/draft", response_class=HTMLResponse, dependencies=_CSRF)
async def outreach_draft_create(
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    if contact.job_id != job.id:
        raise HTTPException(status_code=404)
    if not has_applied(db, applicant.id, job.id):
        return _apply_first(job)
    await find_email(db, contact, job, gateway=gateway, settings=settings, persist=db.commit)
    researched = await research_contact(db, contact, gateway=gateway, settings=settings, persist=db.commit)
    if researched.stopped_by == "budget":
        return RedirectResponse(f"/jobs/{job.id}/outreach?notice=budget", status_code=_SEE_OTHER)
    gmail = await draft_for_contact(
        db,
        applicant_id=applicant.id,
        job=job,
        contact=contact,
        research=researched.research,
        channel=OutreachChannel.gmail,
        gateway=gateway,
        settings=settings,
        persist=db.commit,
    )
    if gmail.draft is None:
        problem = gmail.problem or gmail.stopped_by or "draft_failed"
        return RedirectResponse(f"/jobs/{job.id}/outreach?notice={quote(problem)}", status_code=_SEE_OTHER)
    return RedirectResponse(f"/jobs/{job.id}/outreach/{contact.id}", status_code=_SEE_OTHER)


@router.get("/jobs/{job_id}/outreach/{contact_id}", response_class=HTMLResponse)
def outreach_contact_page(
    request: Request,
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    if contact.job_id != job.id:
        raise HTTPException(status_code=404)
    gmail_draft = db.scalar(
        select(OutreachDraft).where(
            OutreachDraft.applicant_id == applicant.id,
            OutreachDraft.contact_id == contact.id,
            OutreachDraft.channel == OutreachChannel.gmail.value,
        )
    )
    research = db.scalar(select(OutreachResearch).where(OutreachResearch.contact_id == contact.id))
    notice = request.query_params.get("notice")
    return page(
        f"Message · {contact.full_name}",
        _draft_html(request, job, contact, gmail_draft=gmail_draft, research=research, notice=notice),
        header=applicant_bar(request, "matches"),
        narrow=True,
    )


def gmail_compose_url(*, to: str, subject: str, body: str) -> str:
    """Gmail's compose window with the email filled in. The person reads it there and presses Send."""
    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body})


def _draft_html(
    request: Request,
    job: Job,
    contact: OutreachContact,
    *,
    gmail_draft: OutreachDraft | None,
    research: OutreachResearch | None,
    notice: str | None,
) -> str:
    library = load_patterns()
    patterns = library.for_role(contact.role_kind)
    pattern_bits = "".join(f"<li><b>{html.escape(p.label)}</b> — {html.escape(p.rule)}</li>" for p in patterns)
    quotes = ""
    if research and research.quotes:
        quotes = "<ul>" + "".join(f"<li>{html.escape(str(q.get('text') or '')[:200])}</li>" for q in research.quotes[:5]) + "</ul>"
    gmail_form = '<p class="muted">No email drafted yet. Go back and press Research and draft an email.</p>'
    if gmail_draft:
        body = html.escape(current_body(gmail_draft))
        subject = html.escape(gmail_draft.subject or "")
        if contact.email:
            to = f'<p class="muted">To: {html.escape(contact.email)}. {html.escape(email_note(contact))}</p>'
            open_button = (
                '<button type="submit" name="action" value="gmail" class="primary" formtarget="_blank">'
                "Open in Gmail</button>"
            )
        else:
            to = f'<p class="muted">{html.escape(email_note(contact))} Copy the email and add their address yourself.</p>'
            open_button = ""
        gmail_form = f"""
<form method="post" action="/jobs/{job.id}/outreach/{contact.id}/take">
  {csrf_field(request)}
  <input type="hidden" name="draft_id" value="{gmail_draft.id}"/>
  <label>Subject <input name="subject" value="{subject}"/></label>
  <label>Body <textarea name="body" rows="10">{body}</textarea></label>
  {to}
  <p class="hint">Open in Gmail starts a new email in your own Gmail with any edits above.
  Read it there and press Send yourself. Nothing is sent from this site.</p>
  <p>
    {open_button}
    <button type="submit" name="action" value="copy">Copy the email</button>
    <button type="submit" name="action" value="save">Save edits</button>
  </p>
</form>
"""
    return f"""
<p><a href="/jobs/{job.id}/outreach">← Contacts</a></p>
{_error(notice)}
<div class="card">
<h1>{html.escape(contact.full_name)}</h1>
<p>{html.escape(contact.title or "")} · {html.escape(contact.role_kind.replace('_', ' '))}</p>
<p class="muted">Why this tone</p>
<ul>{pattern_bits}</ul>
<p class="muted">Research hooks</p>
{quotes or '<p class="muted">None yet.</p>'}
</div>
<div class="card">
<h2>Email</h2>
{gmail_form}
</div>
"""


@router.post("/jobs/{job_id}/outreach/{contact_id}/take", response_class=HTMLResponse, dependencies=_CSRF)
def outreach_take(
    request: Request,
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    draft_id: uuid.UUID = Form(...),
    body: str = Form(...),
    subject: str = Form(""),
    action: str = Form("save"),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    """Save the edits, then open the email in Gmail or show it to copy. The person sends it themselves."""
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    draft = db.get(OutreachDraft, draft_id)
    if (
        draft is None
        or draft.applicant_id != applicant.id
        or draft.contact_id != contact.id
        or draft.channel != OutreachChannel.gmail.value
    ):
        raise HTTPException(status_code=404)
    draft.user_edited_body = body.strip() or draft.user_edited_body
    draft.subject = subject.strip() or draft.subject
    if action == "save":
        draft.status = OutreachDraftStatus.edited.value
        db.commit()
        return RedirectResponse(f"/jobs/{job.id}/outreach/{contact.id}?notice=saved", status_code=_SEE_OTHER)
    # Taken to send: it no longer counts as waiting on the application card.
    record_copy(db, draft)
    db.commit()
    text = current_body(draft)
    if action == "gmail" and contact.email:
        return RedirectResponse(
            gmail_compose_url(to=contact.email, subject=draft.subject or "", body=text), status_code=_SEE_OTHER
        )
    return page(
        "Copy the email",
        f"""
<p><a href="/jobs/{job.id}/outreach/{contact.id}">← Back</a></p>
<div class="card">
<h1>Email ready to copy</h1>
<p>Paste it into a new email in your own Gmail and send it yourself. This site never sends email for you.</p>
<p class="muted">To: {html.escape(contact.email or "no address found")}</p>
<label>Subject <input readonly value="{html.escape(draft.subject or "")}"/></label>
<label>Body <textarea id="copy-box" rows="10" readonly>{html.escape(text)}</textarea></label>
<script>
const box = document.getElementById("copy-box");
box.focus(); box.select();
navigator.clipboard.writeText(box.value).catch(() => {{}});
</script>
</div>
""",
        header=applicant_bar(request, "matches"),
        narrow=True,
    )


@router.get("/account/gmail", response_class=HTMLResponse)
def gmail_page(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Outreach emails open in the person's own Gmail now; this page only removes a connection made before that."""
    connection = active_connection(db, applicant.id)
    if connection:
        action = f"""<p>An earlier version connected <b>{html.escape(connection.email)}</b>. It is no longer used.</p>
<form method="post" action="/account/gmail/disconnect">
  {csrf_field(request)}
  <button type="submit" class="danger">Disconnect</button>
</form>"""
    else:
        action = "<p>Outreach emails open in your own Gmail for you to send, so there is nothing to connect.</p>"
    body = f"""
<div class="card">
<h1>Gmail</h1>
{_error(request.query_params.get("notice"))}
{action}
<p><a href="/account">← Account</a></p>
</div>
"""
    return page("Gmail", body, header=applicant_bar(request, "account"), narrow=True)


@router.post("/account/gmail/disconnect", dependencies=_CSRF)
def gmail_disconnect(
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    revoke_connection(db, applicant.id)
    db.commit()
    return RedirectResponse("/account/gmail?notice=disconnected", status_code=_SEE_OTHER)
