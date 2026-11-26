"""Outreach after Submitted (D77): people found, emails found, drafts written, LinkedIn notes for the extension."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applications.prepare import application_for_job, mark_submitted
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import (
    OutreachChannel,
    OutreachContact,
    OutreachDraft,
    SpendKind,
    SpendLedger,
)
from app.outreach.auto import drafts_waiting, pick_contacts, prepare_outreach
from app.outreach.email_find import (
    company_domain,
    email_note,
    find_email,
    guesses,
    name_parts,
)
from app.outreach.gmail import record_copy
from tests.jobs import DAY_1, _Apify, _public_resolve
from tests.test_matching import _applicant
from tests.test_outreach import _employee, _matched_job, _policy

NOTE = "Hi Robin, I've just applied for the Junior Developer role at Acme. Could I ask how the team works?"


class _World:
    """Apify people and profiles, Claude drafts and Hunter lookups, answered in one place."""

    def __init__(self, people: list[dict], hunter: dict | None = None, hunter_status: int = 200) -> None:
        self.apify = _Apify(people)
        self.hunter, self.hunter_status = hunter, hunter_status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        if host == "api.apify.com":
            return self.apify(request)
        if host == "api.hunter.io":
            return httpx.Response(self.hunter_status, json={"data": self.hunter or {}})
        answer = {"subject": "Junior Developer application", "body": NOTE, "pattern_ids": []}
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 500, "output_tokens": 60},
            },
        )

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.apify.com", "api.anthropic.com", "api.hunter.io"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )


def _settings(**extra) -> Settings:
    return Settings(
        apify_api_token="test-token",
        anthropic_api_key="test-key",
        ai_provider="anthropic",
        session_secret="x" * 32,
        **extra,
    )


def _contact(session: Session, applicant, job, name: str, title: str, kind: str, minutes: int = 0) -> OutreachContact:
    row = OutreachContact(
        applicant_id=applicant, job_id=job.id, full_name=name, title=title, company_name="Acme",
        role_kind=kind, evidence={}, source_actor="test", created_at=DAY_1 + timedelta(minutes=minutes),
    )
    session.add(row)
    session.flush()
    return row


def test_the_employers_domain_comes_from_its_own_site_never_a_job_board() -> None:
    class _Job:
        def __init__(self, url: str) -> None:
            self.apply_url = url

    assert company_domain(_Job("https://careers.acme.co.uk/jobs/1")) == "acme.co.uk"
    assert company_domain(_Job("https://www.acme.com/apply")) == "acme.com"
    assert company_domain(_Job("https://boards.greenhouse.io/acme/jobs/1")) is None
    assert company_domain(_Job("https://acme.wd3.myworkdayjobs.com/x")) is None
    assert company_domain(_Job("https://www.linkedin.com/jobs/view/9")) is None


def test_guesses_use_the_usual_company_formats() -> None:
    assert name_parts("Dr. Zoë O'Brien-Smith (she/her)") == ("zoe", "obriensmith")
    assert name_parts("Madonna") is None
    assert guesses("Robin Example", "acme.com")[:4] == [
        "robin.example@acme.com", "robin@acme.com", "rexample@acme.com", "robinexample@acme.com",
    ]


async def test_an_email_is_guessed_without_a_provider_key(session: Session) -> None:
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://careers.acme.example/jobs/99")
    contact = _contact(session, applicant, job, "Robin Example", "Engineering Manager", "hiring_manager")
    world = _World([])
    gateway = world.gateway()
    try:
        result = await find_email(session, contact, job, gateway=gateway, settings=_settings(), persist=session.flush)
    finally:
        await gateway.aclose()

    assert result.email == contact.email == "robin.example@acme.example"
    assert contact.evidence["email_source"] == "guessed"
    assert "robin@acme.example" in email_note(contact) and "may bounce" in email_note(contact)
    assert "api.hunter.io" not in world.calls


async def test_hunter_finds_the_email_when_a_key_is_set(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://careers.acme.example/jobs/99")
    contact = _contact(session, applicant, job, "Robin Example", "Engineering Manager", "hiring_manager")
    world = _World([], hunter={"email": "r.example@acme.example", "score": 91})
    gateway = world.gateway()
    try:
        await find_email(
            session, contact, job, gateway=gateway, settings=_settings(hunter_api_key="hk"), persist=session.flush, now=lambda: DAY_1
        )
    finally:
        await gateway.aclose()

    assert contact.email == "r.example@acme.example"
    assert email_note(contact) == "Found by Hunter (91% confident)."
    spend = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.email_lookup)).all()
    assert len(spend) == 1 and spend[0].provider == "hunter"


async def test_a_weak_hunter_answer_falls_back_to_the_guess(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://careers.acme.example/jobs/99")
    contact = _contact(session, applicant, job, "Robin Example", "Engineering Manager", "hiring_manager")
    world = _World([], hunter={"email": "x@acme.example", "score": 20})
    gateway = world.gateway()
    try:
        await find_email(
            session, contact, job, gateway=gateway, settings=_settings(hunter_api_key="hk"), persist=session.flush, now=lambda: DAY_1
        )
    finally:
        await gateway.aclose()

    assert contact.email == "robin.example@acme.example"
    assert contact.evidence["email_source"] == "guessed"


def test_the_hiring_manager_and_people_in_the_role_come_first(session: Session) -> None:
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant)
    ceo = _contact(session, applicant, job, "Ceo Person", "CEO", "executive", 0)
    recruiter = _contact(session, applicant, job, "Rec Person", "Talent Partner", "recruiter", 1)
    peer = _contact(session, applicant, job, "Peer Person", "Software Developer", "employee", 2)
    manager = _contact(session, applicant, job, "Boss Person", "Engineering Manager", "hiring_manager", 3)

    assert pick_contacts([ceo, recruiter, peer, manager], "Junior Developer", 3) == [manager, peer, recruiter]


async def test_submitted_finds_people_and_drafts_without_sending(session: Session) -> None:
    _policy(session)
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant, apply_url="https://careers.acme.example/jobs/99")
    mark_submitted(session, applicant, job)
    world = _World([_employee("Robin", "Acme", "Software Developer")])
    gateway = world.gateway()
    try:
        report = await prepare_outreach(session, gateway=gateway, settings=_settings(), persist=session.flush, now=lambda: DAY_1)
    finally:
        await gateway.aclose()

    application = application_for_job(session, applicant, job.id)
    assert application.outreach_state == "ready" and report.ready == 1
    contact = session.scalars(select(OutreachContact).where(OutreachContact.job_id == job.id)).one()
    assert contact.email == "robin.example@acme.example"
    channels = sorted(d.channel for d in session.scalars(select(OutreachDraft).where(OutreachDraft.contact_id == contact.id)))
    assert channels == [OutreachChannel.gmail.value, OutreachChannel.linkedin_note.value]
    assert drafts_waiting(session, application) == 2
    assert "gmail.googleapis.com" not in world.calls

    # A second cycle does nothing more: the application is no longer queued.
    again = world.gateway()
    try:
        await prepare_outreach(session, gateway=again, settings=_settings(), persist=session.flush, now=lambda: DAY_1)
    finally:
        await again.aclose()
    assert len(session.scalars(select(OutreachDraft)).all()) == 2


async def test_a_spending_cap_leaves_the_application_queued(session: Session) -> None:
    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant)
    mark_submitted(session, applicant, job)
    world = _World([_employee("Robin", "Acme")])
    gateway = world.gateway()
    try:
        report = await prepare_outreach(session, gateway=gateway, settings=_settings(), persist=session.flush, now=lambda: DAY_1)
    finally:
        await gateway.aclose()

    assert report.stopped_by == "budget"
    assert application_for_job(session, applicant, job.id).outreach_state == "queued"


async def test_extension_gets_the_note_the_person_chose_for_linkedin(session: Session) -> None:
    from fastapi.testclient import TestClient

    from app.applications.extension_device import issue_token
    from app.core.db import get_db
    from app.main import app

    applicant = _applicant(session, roles=["Junior Developer"], locations=["Manchester"])
    job = _matched_job(session, applicant)
    contact = _contact(session, applicant, job, "Robin Example", "Software Developer", "employee")
    contact.linkedin_url = "https://www.linkedin.com/in/robin"
    note = OutreachDraft(
        applicant_id=applicant, contact_id=contact.id, job_id=job.id, channel=OutreachChannel.linkedin_note.value,
        subject=None, body=NOTE, pattern_ids=[], evidence_digest="d", status="draft",
    )
    session.add(note)
    session.flush()
    token = issue_token(session, applicant)
    app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/extension/outreach/notes", headers=headers).json() == []
        send = record_copy(session, note)
        assert client.get("/extension/outreach/notes", headers=headers).json() == [
            {"profile_url": "https://www.linkedin.com/in/robin", "note": NOTE}
        ]
        send.created_at = datetime.now(UTC) - timedelta(hours=2)
        session.flush()
        assert client.get("/extension/outreach/notes", headers=headers).json() == []
    finally:
        app.dependency_overrides.clear()
