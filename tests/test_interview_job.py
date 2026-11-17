"""The job pack uses the advert and reuses a pack until its sources change."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic import AiCallResponse
from app.core.config import Settings
from app.interview import job_pack
from app.interview.collect import CollectReport
from app.interview.collect import store_items
from app.interview.plan import PROMPT_VERSION as ROLE_PROMPT_VERSION
from app.models import (
    ApplicantJobMatch,
    BudgetPolicy,
    CompanyResearch,
    CompanyResearchStatus,
    InterviewIntelPack,
    InterviewEvidence,
    InterviewPlatform,
    JobFreshness,
    SalaryStatus,
)
from tests.jobs import _job, _li, _store
from tests.test_matching import _applicant


async def test_job_pack_falls_back_without_company_stories_and_refreshes_on_advert_change(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _store(session, [_li(981, title="Backend Developer", content="Build Python APIs.")], at=now)
    job = _job(session, 981)
    assert job is not None and job.freshness == JobFreshness.open.value
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant_id,
            job_id=job.id,
            score=Decimal("4"),
            reasons={"role": {"target": "Backend Developer"}},
            role_match=True,
            location_fit=True,
            salary_status=SalaryStatus.unknown.value,
        )
    )
    session.add(
        InterviewIntelPack(
            role_key="backend developer",
            role_label="Backend Developer",
            summary="Practise clear examples.",
            source_hash="role-facts",
            model="test",
            prompt_version=ROLE_PROMPT_VERSION,
            ready_at=now,
            expires_at=now + timedelta(days=7),
        )
    )
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=Decimal("10"),
            currency="GBP",
            effective_from=now - timedelta(days=1),
        )
    )
    session.flush()

    company = CompanyResearch(
        company_key="acme",
        company_name="Acme",
        status=CompanyResearchStatus.unavailable.value,
        research={},
        fetched_at=now,
    )

    async def fake_research(*args, **kwargs):
        return company

    async def fake_collect(*args, **kwargs):
        return CollectReport()

    prompts: list[dict] = []

    async def fake_ai(_gateway, body, **_kwargs):
        prompts.append(body)
        return AiCallResponse(
            payload={
                "summary": "Practise Python API examples. Company interview stories were not found.",
                "success_themes": [],
                "failure_themes": [],
                "differentiators": [],
                "materials": [],
                "practice_plan": [],
            },
            prompt_tokens=120,
            completion_tokens=30,
            model="test",
        )

    monkeypatch.setattr(job_pack, "research_company", fake_research)
    monkeypatch.setattr(job_pack, "collect_for_role", fake_collect)
    monkeypatch.setattr(job_pack, "send_anthropic_request", fake_ai)
    settings = Settings(anthropic_api_key="test-key", apify_api_token="test-key")

    first = await job_pack.build_job_pack(
        session, applicant_id=applicant_id, job=job, gateway=object(), settings=settings
    )
    assert first.pack is not None
    assert len(prompts) == 1
    data = json.loads(prompts[0]["messages"][0]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert data["company_interview_stories"] == []
    assert "Build Python APIs" in data["job_advert"]

    again = await job_pack.build_job_pack(
        session, applicant_id=applicant_id, job=job, gateway=object(), settings=settings
    )
    assert again.pack is not None and again.pack.id == first.pack.id
    assert again.pack.prompt_version == "interview-job-v2"
    assert len(prompts) == 1

    again.pack.prompt_version = "interview-job-v1"
    session.flush()
    rebuilt = await job_pack.build_job_pack(
        session, applicant_id=applicant_id, job=job, gateway=object(), settings=settings
    )
    assert rebuilt.pack is not None and rebuilt.pack.id != again.pack.id
    assert rebuilt.pack.prompt_version == "interview-job-v2"
    assert len(prompts) == 2

    job.description = "Build Python APIs and maintain CI pipelines."
    refreshed = await job_pack.build_job_pack(
        session, applicant_id=applicant_id, job=job, gateway=object(), settings=settings
    )
    assert refreshed.pack is not None and refreshed.pack.id != rebuilt.pack.id
    assert len(prompts) == 3

    store_items(
        session,
        InterviewPlatform.reddit,
        [
            {
                "url": "https://reddit.com/r/jobs/981",
                "title": "Acme interview",
                "body": "I got the offer as a backend developer at Acme.",
            }
        ],
        role_key="company:acme:backend developer",
        started_at=now,
        finished_at=now,
        max_snippet=2000,
    )
    story = session.scalar(select(InterviewEvidence).where(InterviewEvidence.role_key == "company:acme:backend developer"))
    assert story is not None
    story.labelled = True
    story.outcome = "success"
    with_story = await job_pack.build_job_pack(
        session, applicant_id=applicant_id, job=job, gateway=object(), settings=settings
    )
    assert with_story.pack is not None and with_story.pack.id != refreshed.pack.id
    company_data = json.loads(prompts[-1]["messages"][0]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert company_data["company_interview_stories"][0]["url"] == story.url
    assert company_data["company_interview_stories"][0]["role_fit"] == "same"
    assert company_data["company_interview_stories"][0]["counts_as_passed"] is True
