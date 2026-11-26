"""Outreach: normalize contacts, fact-check drafts, Apify find, Gmail dry-run."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallResponse
from app.applicants.cv import store_cv
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
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
from app.outreach.draft import draft_for_contact
from app.outreach.find import find_contacts
from app.outreach.gmail import record_copy, save_connection, send_draft
from app.outreach.normalize import (
    linkedin_job_url,
    normalize_people,
    normalize_research,
    parse_email,
    role_kind_for,
)
from app.outreach.seal import seal, unseal
from tests.auth import sign_up
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
    body = apify.bodies()[0]
    assert body["companies"] == ["Acme"]
    assert "companyName" not in body
    # Apify refuses a run capped under $0.05.
    assert float(apify.requests[0].url.params["maxTotalChargeUsd"]) >= 0.05


def _employee(first: str, company: str, title: str = "Talent Partner") -> dict:
    return {
        "firstName": first,
        "lastName": "Example",
        "linkedinUrl": f"https://www.linkedin.com/in/{first.lower()}",
        "currentPositions": [{"companyName": company, "title": title, "current": True}],
    }


async def test_find_contacts_drops_people_at_a_namesake_company(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://acme.example/careers/99")
    items = [
        _employee("Robin", "Acme Ltd"),
        _employee("Sam", "Acme Retail"),
        _employee("Lee", "Other Co"),
        _employee("Kit", "Acme Group"),
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
    assert [c.full_name for c in report.contacts] == ["Robin Example"]
    assert report.contacts[0].title == "Talent Partner"
    assert report.contacts[0].company_name == "Acme Ltd"
    assert report.contacts[0].role_kind == "recruiter"


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


def test_copying_an_email_records_it_as_taken(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant_id)
    contact = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name="Casey",
        role_kind="employee",
        evidence={},
        source_actor="test",
        email="casey@acme.example",
    )
    session.add(contact)
    session.flush()
    draft = OutreachDraft(
        applicant_id=applicant_id,
        contact_id=contact.id,
        job_id=job.id,
        channel=OutreachChannel.gmail.value,
        subject="Hello",
        body="Short email",
        pattern_ids=[],
        evidence_digest="x",
        status=OutreachDraftStatus.draft.value,
    )
    session.add(draft)
    session.flush()
    row = record_copy(session, draft)
    assert row.status == "copied" and row.channel == OutreachChannel.gmail.value
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


async def test_outreach_waits_until_the_person_has_applied(session: Session, monkeypatch) -> None:
    from app.accounts import throttle
    from app.api.forms import require_csrf
    from app.api.outreach import get_gateway
    from app.core.config import get_settings
    from app.core.db import get_db
    from app.main import app
    from app.models import Applicant

    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    get_settings.cache_clear()
    throttle.reset()
    _policy(session)
    apify = _Apify(
        [{"fullName": "Casey Hire", "title": "Hiring Manager", "linkedinUrl": "https://www.linkedin.com/in/casey"}],
        cost_usd=0.05,
    )

    async def _gateway():
        gateway = apify.gateway()
        try:
            yield gateway
        finally:
            await gateway.aclose()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[get_gateway] = _gateway
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            await sign_up(client, "outreach@example.com", "correct horse battery")
            applicant = session.scalar(select(Applicant).where(Applicant.email == "outreach@example.com"))
            job = _matched_job(session, applicant.id)

            refused = await client.post(f"/jobs/{job.id}/outreach/find")
            assert refused.status_code == 303
            assert "applied%20first" in refused.headers["location"]
            assert apify.requests == []
            page = (await client.get(f"/jobs/{job.id}/outreach")).text
            assert "Find people" not in page
            assert "applied" in page

            assert (await client.post(f"/jobs/{job.id}/applied")).status_code == 303
            found = await client.post(f"/jobs/{job.id}/outreach/find")
            assert found.status_code == 303
            assert apify.requests
            contacts = session.scalars(select(OutreachContact).where(OutreachContact.job_id == job.id)).all()
            assert [contact.full_name for contact in contacts] == ["Casey Hire"]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        throttle.reset()


async def test_a_different_chosen_cv_does_not_reuse_the_previous_draft(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def _reply(*_args: object, **_kwargs: object) -> AiCallResponse:
        calls.append(1)
        return AiCallResponse(
            payload={
                "subject": "Backend Engineer",
                "body": f"I would like to talk about the Backend Engineer role at Acme ({len(calls)}).",
                "pattern_ids": [],
            },
            prompt_tokens=1,
            completion_tokens=1,
            model="test",
        )

    monkeypatch.setattr("app.outreach.draft.send_anthropic_request", _reply)
    applicant_id = _applicant(session, roles=["Backend Engineer"], locations=["Manchester"])
    backend = store_cv(session, applicant_id, b"%PDF-1.7\nbackend", label="Backend Engineer")
    analyst = store_cv(session, applicant_id, b"%PDF-1.7\nanalyst", label="Data Analyst")
    job = _matched_job(session, applicant_id)
    job.title = "Backend Engineer"
    job.description = "Build HTTP services."
    contact = OutreachContact(
        applicant_id=applicant_id,
        job_id=job.id,
        full_name="Ada Recruiter",
        company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/ada-draft",
        role_kind="recruiter",
        evidence={},
        source_actor="test",
    )
    session.add(contact)
    session.flush()
    settings = Settings(
        _env_file=None,
        session_secret="x" * 32,
        anthropic_api_key="test-key",
        outreach_ai_unit_price=Decimal("0"),
    )
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset(),
        resolve=_public_resolve,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    try:
        first = await draft_for_contact(
            session,
            applicant_id=applicant_id,
            job=job,
            contact=contact,
            research=None,
            channel=OutreachChannel.gmail,
            gateway=gateway,
            settings=settings,
            persist=session.flush,
        )
        again = await draft_for_contact(
            session,
            applicant_id=applicant_id,
            job=job,
            contact=contact,
            research=None,
            channel=OutreachChannel.gmail,
            gateway=gateway,
            settings=settings,
            persist=session.flush,
        )
        assert first.draft is not None and first.problem is None
        assert again.draft is not None
        assert again.draft.id == first.draft.id
        assert len(calls) == 1

        backend.label = "Data Analyst"
        analyst.label = "Backend Engineer"
        session.flush()
        switched = await draft_for_contact(
            session,
            applicant_id=applicant_id,
            job=job,
            contact=contact,
            research=None,
            channel=OutreachChannel.gmail,
            gateway=gateway,
            settings=settings,
            persist=session.flush,
        )
        assert switched.draft is not None and switched.problem is None
        assert switched.draft.id != first.draft.id
        assert len(calls) == 2
    finally:
        await gateway.aclose()


def test_a_bracketed_company_name_still_counts_as_the_employer() -> None:
    from app.models import OutreachRoleKind
    from app.outreach.normalize import FoundContact, works_at

    def person(company: str) -> FoundContact:
        return FoundContact("A B", "Recruiter", company, None, None, OutreachRoleKind.recruiter, {}, "actor")

    assert works_at(person("TPP (The Phoenix Partnership)"), "TPP")
    assert works_at(person("TPP"), "TPP (The Phoenix Partnership)")
    assert not works_at(person("TPP Recruitment"), "TPP")
    assert not works_at(person("TPP Group"), "TPP")


def test_the_draft_schema_uses_only_keywords_structured_output_accepts() -> None:
    from app.outreach.draft import SCHEMA

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return set(node) | set().union(*(keys(v) for v in node.values()))
        if isinstance(node, list):
            return set().union(*(keys(v) for v in node))
        return set()

    # Anthropic answers 400 to these in output_config.format.schema.
    assert not keys(SCHEMA) & {"maxItems", "minItems", "maxLength", "minLength", "maximum", "minimum"}


def test_a_long_linkedin_note_is_cut_back_to_whole_sentences() -> None:
    from app.outreach.draft import LINKEDIN_MAX, fit_note

    note = "Hi Sam, I'm applying for the Junior Developer role at Acme. " + "I build Python services. " * 12 + "Would you know who hires?"
    fitted = fit_note(note)
    assert len(fitted) <= LINKEDIN_MAX
    assert fitted.startswith("Hi Sam, I'm applying for the Junior Developer role at Acme.")
    assert fitted.endswith("Would you know who hires?")  # The ask is never the part that is cut.
    assert fit_note("Short note.") == "Short note."
    assert not fit_note("word " * 80).endswith("wor")


class _NoteWriter:
    """Anthropic that writes an over-long LinkedIn note first, then a short one when asked again."""

    LONG = "Hi Casey, I'm applying for the Junior Developer role at Acme. " + "I like building things. " * 15 + "Could we talk?"
    SHORT = "Hi Casey, I'm applying for the Junior Developer role at Acme. Could we talk?"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        prompt = json.loads(request.content)["messages"][0]["content"]
        self.prompts.append(prompt)
        body = self.SHORT if "PREVIOUS_NOTE" in prompt else self.LONG
        answer = {"subject": "", "body": body, "pattern_ids": []}
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 500, "output_tokens": 60},
            },
        )


async def test_an_over_long_linkedin_note_is_rewritten_not_cut(session: Session) -> None:
    from app.fetch.gateway import FetchGateway
    from app.outreach.draft import draft_for_contact

    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant)
    contact = OutreachContact(
        applicant_id=applicant, job_id=job.id, full_name="Casey Hire", title="Hiring Manager",
        company_name="Acme", role_kind="hiring_manager", evidence={}, source_actor="test",
    )
    session.add(contact)
    session.flush()
    writer = _NoteWriter()
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.anthropic.com"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(writer),
    )
    try:
        result = await draft_for_contact(
            session,
            applicant_id=applicant,
            job=job,
            contact=contact,
            research=None,
            channel=OutreachChannel.linkedin_note,
            gateway=gateway,
            settings=Settings(anthropic_api_key="test-key", ai_provider="anthropic", session_secret="x" * 32),
            persist=session.flush,
            now=lambda: DAY_1,
        )
    finally:
        await gateway.aclose()
    assert len(writer.prompts) == 2
    assert result.draft is not None, result.problem
    assert result.draft.body == _NoteWriter.SHORT
