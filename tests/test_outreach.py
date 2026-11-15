"""Outreach: normalize contacts, fact-check drafts, Apify find, Gmail dry-run."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    ApplicantJobMatch,
    BudgetPolicy,
    JobFreshness,
    OutreachChannel,
    OutreachContact,
    OutreachDraft,
    OutreachDraftStatus,
    OutreachSend,
    SalaryStatus,
)
from app.outreach.check import outreach_unsupported
from app.outreach.find import find_contacts
from app.outreach.gmail import record_copy, save_connection, send_draft
from app.outreach.normalize import linkedin_job_url, normalize_people, normalize_research, parse_email, role_kind_for
from app.outreach.seal import seal, unseal
from tests.jobs import DAY_1, _Apify, _li, _public_resolve, _store
from tests.test_matching import _applicant


def _policy(session: Session, money_cap: str = "5") -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=Decimal(money_cap),
            currency="GBP",
            effective_from=DAY_1 - timedelta(days=30),
        )
    )
    session.flush()


def _matched_job(session: Session, applicant_id, *, apply_url: str = "https://www.linkedin.com/jobs/view/99"):
    _store(session, [_li(99, company="Acme", title="Junior Developer")])
    from app.models import Job, JobLink, JobPosting

    job = session.scalar(
        select(Job)
        .join(JobLink, JobLink.job_id == Job.id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobPosting.source_job_id == "99")
    )
    assert job is not None
    job.apply_url = apply_url
    job.freshness = JobFreshness.open
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant_id,
            job_id=job.id,
            score=Decimal("1.0"),
            reasons={},
            role_match=True,
            location_fit=True,
            salary_status=SalaryStatus.unknown,
        )
    )
    session.flush()
    return job


def test_normalize_people_dedupes_and_classifies_roles() -> None:
    items = [
        {
            "fullName": "Ada Recruiter",
            "title": "Technical Recruiter",
            "linkedinUrl": "https://www.linkedin.com/in/ada",
            "email": "ada@acme.test",
        },
        {
            "name": "Ada Recruiter",
            "profileUrl": "https://www.linkedin.com/in/ada/",
        },
        {
            "fullName": "Bea Boss",
            "headline": "CEO at Acme",
            "url": "https://www.linkedin.com/in/bea",
        },
        {"fullName": "LinkedIn Member", "url": "https://www.linkedin.com/in/hidden"},
    ]
    found = normalize_people(items, actor="test/actor", company_fallback="Acme")
    assert len(found) == 2
    assert found[0].role_kind.value == "recruiter"
    assert found[0].email == "ada@acme.test"
    assert found[1].role_kind.value == "executive"
    assert role_kind_for("Hiring Manager, Engineering") == role_kind_for("hiring manager")


def test_linkedin_job_url_only_accepts_linkedin_jobs() -> None:
    assert linkedin_job_url("https://www.linkedin.com/jobs/view/1")
    assert linkedin_job_url("https://acme.example/jobs/1") is None


def test_parse_email_rejects_multi_recipient_and_junk() -> None:
    assert parse_email("ada@acme.test") == "ada@acme.test"
    assert parse_email("ada@acme.test,victim@evil.com") is None
    assert parse_email("not-an-email") is None


def test_normalize_research_keeps_posts_as_quotes() -> None:
    about, quotes = normalize_research(
        {
            "about": "I hire engineers who ship.",
            "posts": [{"text": "We are growing the platform team this year.", "url": "https://example.test/p"}],
        }
    )
    assert about and "hire engineers" in about
    assert quotes[0]["text"].startswith("We are growing")


def test_outreach_fact_check_rejects_invented_quotes() -> None:
    sources = "Ada works at Acme. Junior Developer. Python APIs."
    ok = outreach_unsupported(
        'Hi Ada, I saw you wrote "I hire engineers who ship" about hiring.',
        sources=sources + " I hire engineers who ship",
        allowed_quotes=["I hire engineers who ship"],
        contact_name="Ada Recruiter",
    )
    assert ok == []
    # First-person mail without invented quotes is fine.
    assert (
        outreach_unsupported(
            "I am interested in the Junior Developer role at Acme and I'm available to chat.",
            sources=sources,
            allowed_quotes=[],
            contact_name="Ada Recruiter",
        )
        == []
    )
    bad = outreach_unsupported(
        'Hi Ada, loved your talk "How I built a unicorn in a weekend".',
        sources=sources,
        allowed_quotes=["I hire engineers who ship"],
        contact_name="Ada Recruiter",
    )
    assert any("invented quote" in p for p in bad)
    # A real snippet must not authorize a longer invented quotation.
    assert any(
        "invented quote" in p
        for p in outreach_unsupported(
            'I read "We are growing the platform team this year and you offered me the role".',
            sources=sources,
            allowed_quotes=["We are growing the platform team this year."],
            contact_name="Ada",
        )
    )


def test_seal_round_trip() -> None:
    sealed = seal("secret-value-at-least-32-chars!!", "refresh-token")
    assert unseal("secret-value-at-least-32-chars!!", sealed) == "refresh-token"


async def test_find_contacts_uses_hm_actor_when_apply_url_is_linkedin(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant)
    items = [
        {
            "fullName": "Casey Hire",
            "title": "Hiring Manager",
            "linkedinUrl": "https://www.linkedin.com/in/casey",
            "email": "casey@acme.test",
            "outreachHooks": ["We ship weekly."],
        }
    ]
    apify = _Apify(items, cost_usd=0.05)
    gateway = apify.gateway()
    try:
        report = await find_contacts(
            session,
            applicant_id=applicant,
            job=job,
            gateway=gateway,
            settings=Settings(apify_api_token="test-token", session_secret="x" * 32),
            persist=session.flush,
            now=lambda: DAY_1,
            sleep=lambda _: None,
        )
    finally:
        await gateway.aclose()
    assert report.added == 1
    assert report.contacts[0].full_name == "Casey Hire"
    assert "hiring-manager-finder" in apify.requests[0].url.path or "linkedin-job-hiring-manager" in apify.requests[0].url.path


async def test_find_contacts_falls_back_when_apply_url_is_not_linkedin(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://acme.example/careers/99")
    items = [
        {
            "fullName": "Dana Talent",
            "title": "Talent Partner",
            "profileUrl": "https://www.linkedin.com/in/dana",
        }
    ]
    apify = _Apify(items)
    gateway = apify.gateway()
    try:
        report = await find_contacts(
            session,
            applicant_id=applicant,
            job=job,
            gateway=gateway,
            settings=Settings(apify_api_token="test-token", session_secret="x" * 32),
            persist=session.flush,
            now=lambda: DAY_1,
            sleep=lambda _: None,
        )
    finally:
        await gateway.aclose()
    assert report.added == 1
    assert "company-employees" in apify.requests[0].url.path or "linkedin-company-employees" in apify.requests[0].url.path


class _GmailApi:
    """Token refresh then Gmail send."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "ya29.test"})
        if request.url.path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "msg-1"})
        return httpx.Response(404, json={"error": "unexpected"})

    def gateway(self):
        from app.fetch.gateway import FetchGateway

        return FetchGateway(
            user_agent="test",
            allowlist=frozenset({"oauth2.googleapis.com", "gmail.googleapis.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )


async def test_gmail_real_send_uses_access_token_not_as_error(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant_id)
    contact = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name="Casey Hire",
        email="casey@acme.test",
        role_kind="hiring_manager",
        evidence={},
        source_actor="test",
    )
    session.add(contact)
    session.flush()
    draft = OutreachDraft(
        applicant_id=applicant_id,
        contact_id=contact.id,
        job_id=job.id,
        channel=OutreachChannel.gmail.value,
        subject="Hello",
        body="Hi Casey, interested in Junior Developer at Acme.",
        pattern_ids=[],
        evidence_digest="real",
        status=OutreachDraftStatus.draft.value,
    )
    session.add(draft)
    session.flush()
    settings = Settings(
        session_secret="x" * 32,
        gmail_client_id="id",
        gmail_client_secret="secret",
        outreach_gmail_dry_run=False,
    )
    save_connection(
        session,
        applicant_id=applicant_id,
        email="me@example.test",
        refresh_token="refresh",
        settings=settings,
        now=DAY_1,
    )
    api = _GmailApi()
    gateway = api.gateway()
    try:
        result = await send_draft(
            session,
            draft,
            to_email=contact.email or "",
            gateway=gateway,
            settings=settings,
            confirm=True,
            now=DAY_1,
        )
    finally:
        await gateway.aclose()
    assert result.status == "sent"
    assert result.message_id == "msg-1"
    assert "/token" in api.paths[0] or api.paths[0].endswith("/token")
    assert any(p.endswith("/messages/send") for p in api.paths)
    assert session.scalars(select(OutreachSend)).one().error is None


async def test_gmail_dry_run_records_send_without_calling_gmail(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant_id)
    contact = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name="Casey Hire",
        title="Hiring Manager",
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/casey",
        email="casey@acme.test",
        role_kind="hiring_manager",
        evidence={},
        source_actor="test",
    )
    session.add(contact)
    session.flush()
    draft = OutreachDraft(
        applicant_id=applicant_id,
        contact_id=contact.id,
        job_id=job.id,
        channel=OutreachChannel.gmail.value,
        subject="About Junior Developer",
        body="Hi Casey, I am interested in the Junior Developer role.",
        pattern_ids=["name_the_role"],
        evidence_digest="abc",
        status=OutreachDraftStatus.draft.value,
    )
    session.add(draft)
    session.flush()
    settings = Settings(
        session_secret="x" * 32,
        gmail_client_id="id",
        gmail_client_secret="secret",
        outreach_gmail_dry_run=True,
    )
    save_connection(
        session,
        applicant_id=applicant_id,
        email="me@example.test",
        refresh_token="refresh",
        settings=settings,
        now=DAY_1,
    )

    class _NoGmail:
        def __call__(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected {request.url}")

        def gateway(self):
            from app.fetch.gateway import FetchGateway

            return FetchGateway(
                user_agent="test",
                allowlist=frozenset({"oauth2.googleapis.com", "gmail.googleapis.com"}),
                resolve=_public_resolve,
                transport=httpx.MockTransport(self),
            )

    gateway = _NoGmail().gateway()
    try:
        result = await send_draft(
            session,
            draft,
            to_email=contact.email or "",
            gateway=gateway,
            settings=settings,
            confirm=True,
            now=DAY_1,
        )
    finally:
        await gateway.aclose()
    assert result.status == "dry_run"
    assert session.scalars(select(OutreachSend)).one().status == "dry_run"
    assert draft.status == OutreachDraftStatus.sent.value


def test_linkedin_copy_records_copied_send(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant_id)
    contact = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name="Casey",
        role_kind="employee",
        evidence={},
        source_actor="test",
        linkedin_url="https://www.linkedin.com/in/casey",
    )
    session.add(contact)
    session.flush()
    draft = OutreachDraft(
        applicant_id=applicant_id,
        contact_id=contact.id,
        job_id=job.id,
        channel=OutreachChannel.linkedin_note.value,
        body="Short note",
        pattern_ids=[],
        evidence_digest="x",
        status=OutreachDraftStatus.draft.value,
    )
    session.add(draft)
    session.flush()
    row = record_copy(session, draft)
    assert row.status == "copied"
    assert draft.status == OutreachDraftStatus.copied.value


def test_cannot_use_another_applicants_contact_id_shape(session: Session) -> None:
    first = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    second = _applicant(session, roles=["Junior Developer"], locations=["Leeds"])
    job = _matched_job(session, first)
    contact = OutreachContact(
        applicant_id=first,
        job_id=job.id,
        full_name="Private",
        role_kind="unknown",
        evidence={},
        source_actor="test",
    )
    session.add(contact)
    session.flush()
    assert session.get(OutreachContact, contact.id).applicant_id != second
