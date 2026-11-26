"""Outreach UI: find contacts, draft, edit, Gmail send, LinkedIn copy."""

from __future__ import annotations

import html
import secrets
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from app.outreach.find import find_contacts
from app.outreach.gmail import (
    active_connection,
    authorize_url,
    exchange_code,
    gmail_configured,
    profile_email,
    record_copy,
    revoke_connection,
    save_connection,
    send_draft,
)
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


def _error(message: str | None) -> str:
    return f'<p class="error">{html.escape(message)}</p>' if message else ""


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
        email = html.escape(contact.email) if contact.email else "no email found"
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
    <button type="submit" class="primary">Research and draft</button>
  </form>
</div>"""
        )
    body = "\n".join(rows) if rows else '<p class="muted">No contacts stored yet.</p>'
    return f"""
<p><a href="/jobs/{job.id}">← Back to job</a></p>
<div class="card">
<h1>Outreach · {html.escape(job.title)}</h1>
<p class="muted">{html.escape(job.company_name)}. Contacts come from Apify LinkedIn people actors.
You review every message before anything is sent. Cold email is your responsibility under UK PECR.</p>
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
    notice = report.stopped_by or report.problem or f"Found {report.added} new contact(s)."
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
    note = await draft_for_contact(
        db,
        applicant_id=applicant.id,
        job=job,
        contact=contact,
        research=researched.research,
        channel=OutreachChannel.linkedin_note,
        gateway=gateway,
        settings=settings,
        persist=db.commit,
    )
    draft = gmail.draft or note.draft
    if draft is None:
        problem = gmail.problem or note.problem or gmail.stopped_by or note.stopped_by or "draft_failed"
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
    drafts = list(
        db.scalars(
            select(OutreachDraft).where(
                OutreachDraft.applicant_id == applicant.id,
                OutreachDraft.contact_id == contact.id,
            )
        )
    )
    research = db.scalar(select(OutreachResearch).where(OutreachResearch.contact_id == contact.id))
    gmail_draft = next((d for d in drafts if d.channel == OutreachChannel.gmail.value), None)
    note_draft = next((d for d in drafts if d.channel == OutreachChannel.linkedin_note.value), None)
    connection = active_connection(db, applicant.id)
    notice = request.query_params.get("notice")
    return page(
        f"Message · {contact.full_name}",
        _draft_html(
            request,
            job,
            contact,
            gmail_draft=gmail_draft,
            note_draft=note_draft,
            research=research,
            connection_email=connection.email if connection else None,
            gmail_ready=gmail_configured(get_settings()),
            notice=notice,
        ),
        header=applicant_bar(request, "matches"),
        narrow=True,
    )


def _draft_html(
    request: Request,
    job: Job,
    contact: OutreachContact,
    *,
    gmail_draft: OutreachDraft | None,
    note_draft: OutreachDraft | None,
    research: OutreachResearch | None,
    connection_email: str | None,
    gmail_ready: bool,
    notice: str | None,
) -> str:
    library = load_patterns()
    patterns = library.for_role(contact.role_kind)
    pattern_bits = "".join(f"<li><b>{html.escape(p.label)}</b> — {html.escape(p.rule)}</li>" for p in patterns)
    quotes = ""
    if research and research.quotes:
        quotes = "<ul>" + "".join(f"<li>{html.escape(str(q.get('text') or '')[:200])}</li>" for q in research.quotes[:5]) + "</ul>"
    gmail_form = "<p class=\"muted\">No Gmail draft yet. Press Research and draft again.</p>"
    if gmail_draft:
        body = html.escape(current_body(gmail_draft))
        subject = html.escape(gmail_draft.subject or "")
        send_disabled = "" if (connection_email and contact.email) else "disabled"
        connect_hint = (
            f"<p class=\"muted\">Sending as {html.escape(connection_email)}.</p>"
            if connection_email
            else '<p class="muted">Connect Gmail on <a href="/account/gmail">your account</a> first.</p>'
        )
        if not contact.email:
            connect_hint += '<p class="muted">No email on this contact — Gmail send needs one from Apify.</p>'
        if not gmail_ready:
            connect_hint = '<p class="muted">Gmail OAuth is not configured on this server.</p>'
        gmail_form = f"""
<form method="post" action="/jobs/{job.id}/outreach/{contact.id}/send">
  {csrf_field(request)}
  <input type="hidden" name="draft_id" value="{gmail_draft.id}"/>
  <label>Subject <input name="subject" value="{subject}"/></label>
  <label>Body <textarea name="body" rows="10">{body}</textarea></label>
  {connect_hint}
  <label><input type="checkbox" name="confirm" value="1"/> I confirm I may email this person and the text above is what to send.</label>
  <p>
    <button type="submit" name="action" value="save">Save edits</button>
    <button type="submit" name="action" value="send" class="primary" {send_disabled}>Send with Gmail</button>
  </p>
</form>
"""
    note_form = "<p class=\"muted\">No LinkedIn note yet.</p>"
    if note_draft:
        note_body = html.escape(current_body(note_draft))
        profile = (
            f'<p><a class="button" href="{html.escape(contact.linkedin_url, quote=True)}" rel="noopener noreferrer" target="_blank">Open LinkedIn profile</a></p>'
            if contact.linkedin_url
            else '<p class="muted">No LinkedIn profile URL.</p>'
        )
        note_form = f"""
<form method="post" action="/jobs/{job.id}/outreach/{contact.id}/save">
  {csrf_field(request)}
  <input type="hidden" name="draft_id" value="{note_draft.id}"/>
  <label>LinkedIn note <textarea name="body" rows="4" maxlength="300">{note_body}</textarea></label>
  <p><button type="submit">Save note</button></p>
</form>
{profile}
<form method="post" action="/jobs/{job.id}/outreach/{contact.id}/copy">
  {csrf_field(request)}
  <input type="hidden" name="draft_id" value="{note_draft.id}"/>
  <button type="submit" class="primary">Copy LinkedIn note</button>
</form>
<pre id="note-text" style="display:none">{note_body}</pre>
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
<h2>Gmail</h2>
{gmail_form}
</div>
<div class="card">
<h2>LinkedIn (manual)</h2>
{note_form}
</div>
"""


@router.post("/jobs/{job_id}/outreach/{contact_id}/save", response_class=HTMLResponse, dependencies=_CSRF)
def outreach_save(
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    draft_id: uuid.UUID = Form(...),
    body: str = Form(...),
    subject: str | None = Form(None),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    draft = db.get(OutreachDraft, draft_id)
    if draft is None or draft.applicant_id != applicant.id or draft.contact_id != contact.id:
        raise HTTPException(status_code=404)
    draft.user_edited_body = body.strip()
    if draft.channel == OutreachChannel.gmail.value and subject is not None:
        draft.subject = subject.strip() or draft.subject
    draft.status = OutreachDraftStatus.edited.value
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}/outreach/{contact.id}?notice=saved", status_code=_SEE_OTHER)


@router.post("/jobs/{job_id}/outreach/{contact_id}/send", response_class=HTMLResponse, dependencies=_CSRF)
async def outreach_send(
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    draft_id: uuid.UUID = Form(...),
    body: str = Form(...),
    subject: str | None = Form(None),
    confirm: str | None = Form(None),
    action: str = Form("send"),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    draft = db.get(OutreachDraft, draft_id)
    if draft is None or draft.applicant_id != applicant.id or draft.contact_id != contact.id:
        raise HTTPException(status_code=404)
    draft.user_edited_body = body.strip()
    if draft.channel == OutreachChannel.gmail.value and subject is not None:
        draft.subject = subject.strip() or draft.subject
    draft.status = OutreachDraftStatus.edited.value
    if action != "send":
        db.commit()
        return RedirectResponse(f"/jobs/{job.id}/outreach/{contact.id}?notice=saved", status_code=_SEE_OTHER)
    result = await send_draft(
        db,
        draft,
        to_email=contact.email or "",
        gateway=gateway,
        settings=settings,
        confirm=confirm == "1",
    )
    db.commit()
    return RedirectResponse(
        f"/jobs/{job.id}/outreach/{contact.id}?notice={quote(result.error or result.status)}",
        status_code=_SEE_OTHER,
    )


@router.post("/jobs/{job_id}/outreach/{contact_id}/copy", response_class=HTMLResponse, dependencies=_CSRF)
def outreach_copy(
    request: Request,
    job_id: uuid.UUID,
    contact_id: uuid.UUID,
    draft_id: uuid.UUID = Form(...),
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = _open_matched_job(db, applicant, job_id)
    contact = _own_contact(db, applicant, contact_id)
    draft = db.get(OutreachDraft, draft_id)
    if draft is None or draft.applicant_id != applicant.id or draft.contact_id != contact.id:
        raise HTTPException(status_code=404)
    record_copy(db, draft)
    db.commit()
    text = current_body(draft)
    profile = (
        f'<p><a href="{html.escape(contact.linkedin_url, quote=True)}" rel="noopener noreferrer" target="_blank">Open profile</a></p>'
        if contact.linkedin_url
        else ""
    )
    return page(
        "Copy LinkedIn note",
        f"""
<p><a href="/jobs/{job.id}/outreach/{contact.id}">← Back</a></p>
<div class="card">
<h1>LinkedIn note ready</h1>
<p>Select the text and copy it, then paste into LinkedIn yourself. This app never sends LinkedIn messages.</p>
<textarea id="copy-box" rows="6" readonly>{html.escape(text)}</textarea>
{profile}
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
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    connection = active_connection(db, applicant.id)
    notice = request.query_params.get("notice")
    if not gmail_configured(settings):
        body = f"""
<div class="card">
<h1>Gmail</h1>
{_error(notice)}
<p class="muted">Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REDIRECT_URI to connect.</p>
<p><a href="/account">← Account</a></p>
</div>
"""
    elif connection:
        body = f"""
<div class="card">
<h1>Gmail</h1>
{_error(notice)}
<p>Connected as <b>{html.escape(connection.email)}</b>.</p>
<form method="post" action="/account/gmail/disconnect">
  {csrf_field(request)}
  <button type="submit" class="danger">Disconnect</button>
</form>
<p><a href="/account">← Account</a></p>
</div>
"""
    else:
        body = f"""
<div class="card">
<h1>Gmail</h1>
{_error(notice)}
<p>Connect Gmail so you can send approved outreach drafts. Tokens are sealed at rest. Dry-run is
{"on" if settings.outreach_gmail_dry_run else "off"}.</p>
<form method="post" action="/account/gmail/connect">
  {csrf_field(request)}
  <button type="submit" class="primary">Connect Gmail</button>
</form>
<p><a href="/account">← Account</a></p>
</div>
"""
    return page("Gmail", body, header=applicant_bar(request, "account"), narrow=True)


@router.post("/account/gmail/connect", dependencies=_CSRF)
def gmail_connect(
    request: Request,
    applicant: Applicant = Depends(require_login),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not gmail_configured(settings):
        return RedirectResponse("/account/gmail?notice=not_configured", status_code=_SEE_OTHER)
    state = secrets.token_urlsafe(24)
    request.session["gmail_oauth_state"] = state
    request.session["gmail_oauth_applicant"] = str(applicant.id)
    return RedirectResponse(authorize_url(settings, state=state), status_code=_SEE_OTHER)


@router.get("/account/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    gateway: FetchGateway = Depends(get_gateway),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/account/gmail?notice={quote(error)}", status_code=_SEE_OTHER)
    expected = request.session.get("gmail_oauth_state")
    if not code or not state or state != expected or request.session.get("gmail_oauth_applicant") != str(applicant.id):
        return RedirectResponse("/account/gmail?notice=state_mismatch", status_code=_SEE_OTHER)
    request.session.pop("gmail_oauth_state", None)
    request.session.pop("gmail_oauth_applicant", None)
    tokens = await exchange_code(gateway, settings, code=code)
    if isinstance(tokens, str):
        return RedirectResponse(f"/account/gmail?notice={quote(tokens)}", status_code=_SEE_OTHER)
    email = await profile_email(gateway, str(tokens["access_token"]))
    if not email:
        return RedirectResponse("/account/gmail?notice=no_email", status_code=_SEE_OTHER)
    save_connection(
        db,
        applicant_id=applicant.id,
        email=email,
        refresh_token=str(tokens["refresh_token"]),
        settings=settings,
    )
    db.commit()
    return RedirectResponse("/account/gmail?notice=connected", status_code=_SEE_OTHER)


@router.post("/account/gmail/disconnect", dependencies=_CSRF)
def gmail_disconnect(
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    revoke_connection(db, applicant.id)
    db.commit()
    return RedirectResponse("/account/gmail?notice=disconnected", status_code=_SEE_OTHER)
