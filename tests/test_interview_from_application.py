"""Interview practice from an application card, end to end (D77)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallResponse
from app.applications.prepare import mark_submitted
from app.core.config import Settings
from app.interview import job_pack, service
from app.interview.collect import CollectReport
from app.models import (
    Applicant,
    BudgetPolicy,
    CompanyResearch,
    CompanyResearchStatus,
    InterviewIntelPack,
    InterviewIntelRequest,
    InterviewJobPack,
    Job,
    JobFreshness,
)
from tests.auth import sign_up
from tests.test_cv_tailor import client  # noqa: F401  (fixture)


async def _applied_to_a_closed_job(session: Session, client: httpx.AsyncClient) -> tuple[Applicant, Job]:  # noqa: F811
    from tests.test_judge import _jobs

    email = "interview-card@example.com"
    assert (await sign_up(client, email, "correct horse battery")).status_code == 303
    applicant = session.scalar(select(Applicant).where(Applicant.email == email))
    await _jobs(session, ("Data Analyst", "Leeds, UK"))
    job = session.scalar(select(Job).where(Job.title == "Data Analyst"))
    mark_submitted(session, applicant.id, job)
    # Interviews come after the advert closes; the job is not one of the person's target roles either.
    job.freshness = JobFreshness.closed.value
    session.add(BudgetPolicy(provider="*", period="month", money_cap=Decimal(10), currency="GBP",
                             effective_from=datetime.now(UTC) - timedelta(days=1)))
    session.flush()
    return applicant, job


def _fake_pack_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    company = CompanyResearch(
        company_key="acme", company_name="Acme", status=CompanyResearchStatus.unavailable.value, research={},
        fetched_at=datetime.now(UTC),
    )

    async def fake_research(*_args, **_kwargs):
        return company

    async def fake_collect(*_args, **_kwargs):
        return CollectReport()

    async def fake_ai(_gateway, _body, **_kwargs):
        return AiCallResponse(
            payload={
                "summary": "Practise SQL and dashboard examples.",
                "success_themes": [], "failure_themes": [], "differentiators": [], "materials": [], "practice_plan": [],
            },
            prompt_tokens=100, completion_tokens=20, model="test",
        )

    monkeypatch.setattr(job_pack, "research_company", fake_research)
    monkeypatch.setattr(job_pack, "collect_for_role", fake_collect)
    monkeypatch.setattr(job_pack, "send_anthropic_request", fake_ai)


async def test_one_click_on_the_card_gets_a_practice_pack(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    applicant, job = await _applied_to_a_closed_job(session, client)

    cards = (await client.get("/applications")).text
    assert f'href="/jobs/{job.id}/interview">Prepare for interview' in cards
    assert (await client.get(f"/jobs/{job.id}/interview")).status_code == 200

    # No role research yet: one press queues it for this job and the page says it is on its way.
    pressed = await client.post(f"/jobs/{job.id}/interview")
    assert pressed.status_code == 303
    request = session.scalar(select(InterviewIntelRequest).where(InterviewIntelRequest.applicant_id == applicant.id))
    assert request.job_id == job.id and request.role == "Data Analyst"
    assert "Researching interviews for this role" in (await client.get(f"/jobs/{job.id}/interview")).text

    # The worker's role research finishes (a fresh role pack appears); it then builds this job's pack itself.
    now = datetime.now(UTC)
    session.add(InterviewIntelPack(
        role_key="data analyst", role_label="Data Analyst", summary="Role facts.", source_hash="r", model="test",
        prompt_version="test", ready_at=now, expires_at=now + timedelta(days=7),
    ))
    session.flush()
    _fake_pack_calls(monkeypatch)
    settings = Settings(anthropic_api_key="test-key", apify_api_token="test-key", session_secret="x" * 32)
    await service.process_requests(session, gateway=object(), settings=settings, persist=session.flush)

    assert session.scalar(select(InterviewJobPack).where(InterviewJobPack.job_id == job.id)) is not None
    assert request.job_id is None
    page = (await client.get(f"/jobs/{job.id}/interview")).text
    assert "Practise SQL and dashboard examples." in page


async def test_a_closed_job_nobody_applied_to_stays_hidden(
    client: httpx.AsyncClient, session: Session  # noqa: F811
) -> None:
    from tests.test_judge import _jobs

    assert (await sign_up(client, "no-apply@example.com", "correct horse battery")).status_code == 303
    await _jobs(session, ("Data Analyst", "Leeds, UK"))
    job = session.scalar(select(Job).where(Job.title == "Data Analyst"))
    job.freshness = JobFreshness.closed.value
    session.flush()

    assert (await client.get(f"/jobs/{job.id}/interview")).status_code == 404


async def test_two_jobs_for_one_role_both_get_a_pack_from_one_research(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from tests.jobs import _store
    from tests.test_judge import ADVERT, _posting

    applicant, first = await _applied_to_a_closed_job(session, client)
    _store(session, [_posting(2, title="Data Analyst", city="Manchester, UK", content=ADVERT)])
    second = session.scalars(select(Job).where(Job.title == "Data Analyst", Job.id != first.id)).first()
    mark_submitted(session, applicant.id, second)
    session.flush()

    assert (await client.post(f"/jobs/{first.id}/interview")).status_code == 303
    assert (await client.post(f"/jobs/{second.id}/interview")).status_code == 303
    requests = session.scalars(select(InterviewIntelRequest).where(InterviewIntelRequest.applicant_id == applicant.id)).all()
    assert {r.job_id for r in requests} == {first.id, second.id}
    assert "Researching interviews" in (await client.get(f"/jobs/{second.id}/interview")).text

    researched: list[str] = []

    async def fake_role_collect(_session, **kwargs):
        researched.append(kwargs["role_key"])
        now = datetime.now(UTC)
        _session.add(InterviewIntelPack(
            role_key="data analyst", role_label="Data Analyst", summary="Role facts.", source_hash="r",
            model="test", prompt_version="test", ready_at=now, expires_at=now + timedelta(days=7),
        ))
        _session.flush()
        return CollectReport()

    async def fake_analyse(*_args, **_kwargs):
        from app.interview.analyse import AnalyseReport

        return AnalyseReport()

    async def fake_write(_session, **_kwargs):
        from app.interview.plan import fresh_pack

        class _Planned:
            pack = fresh_pack(_session, "data analyst", now=datetime.now(UTC))
            stopped_by = None
            grounding_problems = ()

        return _Planned()

    monkeypatch.setattr(service, "collect_for_role", fake_role_collect)
    monkeypatch.setattr(service, "analyse_evidence", fake_analyse)
    monkeypatch.setattr(service, "write_pack", fake_write)
    _fake_pack_calls(monkeypatch)
    settings = Settings(anthropic_api_key="test-key", apify_api_token="test-key", session_secret="x" * 32)
    await service.process_requests(session, gateway=object(), settings=settings, persist=session.flush)

    assert researched == ["data analyst"]  # The role was researched once for both jobs.
    built = {p.job_id for p in session.scalars(select(InterviewJobPack))}
    assert built == {first.id, second.id}


async def test_the_page_says_when_research_is_paused_or_failed(
    client: httpx.AsyncClient, session: Session  # noqa: F811
) -> None:
    applicant, job = await _applied_to_a_closed_job(session, client)
    await client.post(f"/jobs/{job.id}/interview")
    request = session.scalar(select(InterviewIntelRequest).where(InterviewIntelRequest.applicant_id == applicant.id))

    request.error = "budget"
    session.flush()
    paused = (await client.get(f"/jobs/{job.id}/interview")).text
    assert "monthly spending cap is reached" in paused and "Build or refresh" not in paused

    request.status = "failed"
    request.error = "Provider is not configured"
    session.flush()
    failed = (await client.get(f"/jobs/{job.id}/interview")).text
    assert "not set up on this server" in failed and "Build or refresh" in failed
