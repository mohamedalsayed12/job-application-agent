"""Smoke-test each applicant journey stage on its real route. The /workflow hub is gone."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.applicants.cv import store_cv
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import (
    Applicant,
    Job,
    JobReport,
    TailoredCv,
    TailoredCvStatus,
)
from tests.auth import sign_up
from tests.jobs import _job, _li, _store, show_to

EMAIL = "stages@example.com"
PASSWORD = "correct horse battery"
PROFILE = {
    "target_roles": "Junior Backend Developer",
    "locations": "Manchester",
    "experience_levels": "entry",
    "skills": "Python",
}


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    throttle.reset()

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as value:
        yield value
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _signed_in(client: httpx.AsyncClient, session: Session) -> Applicant:
    assert (await sign_up(client, EMAIL, PASSWORD)).status_code == 303
    applicant = session.scalar(select(Applicant).where(Applicant.email == EMAIL))
    assert applicant is not None
    return applicant


def _matched_open_job(session: Session, applicant: Applicant) -> Job:
    from app.models import ApplicantJobMatch

    _store(
        session,
        [
            _li(
                6101,
                title="Junior Backend Developer",
                content="Build payment services in Python and PostgreSQL for our platform team. " * 4,
                **{"applyUrl": "https://careers.example.com/jobs/6101/apply"},
            )
        ],
    )
    job = _job(session, 6101)
    assert job is not None
    show_to(session, applicant.id, job.id)
    # Listings' default "roles" filter needs a role.kind; empty reasons are hidden.
    match = session.scalar(
        select(ApplicantJobMatch).where(
            ApplicantJobMatch.applicant_id == applicant.id, ApplicantJobMatch.job_id == job.id
        )
    )
    assert match is not None
    match.reasons = {"role": {"target": "Junior Backend Developer", "kind": "exact"}}
    match.score = Decimal("4")
    session.flush()
    return job


async def test_workflow_hub_is_gone(client: httpx.AsyncClient) -> None:
    anonymous = await client.get("/workflow", follow_redirects=False)
    assert anonymous.status_code == 404

    await sign_up(client, EMAIL, PASSWORD)
    logged_in = await client.get("/workflow", follow_redirects=False)
    assert logged_in.status_code == 404
    nav = (await client.get("/listings")).text
    assert 'href="/workflow"' not in nav
    assert ">Workflow<" not in nav


async def test_stage_routes_cover_the_full_applicant_journey(
    client: httpx.AsyncClient, session: Session
) -> None:
    applicant = await _signed_in(client, session)

    # Profile
    profile = await client.get("/profile")
    assert profile.status_code == 200
    assert "target" in profile.text.lower() or "role" in profile.text.lower()
    store_cv(session, applicant.id, b"%PDF-1.7\nstages cv")
    assert (await client.post("/profile", data=PROFILE)).status_code == 303
    saved = await client.get("/profile")
    assert saved.status_code == 200
    assert "Junior Backend Developer" in saved.text

    # Matches
    job = _matched_open_job(session, applicant)
    listings = await client.get("/listings")
    assert listings.status_code == 200
    assert "Junior Backend Developer" in listings.text
    assert f'href="/jobs/{job.id}"' in listings.text

    # Fit report / tailored CV on the job page
    session.add(
        JobReport(
            applicant_id=applicant.id,
            job_id=job.id,
            verdict="Apply",
            score=Decimal("4.0"),
            report={"verdict": "Apply", "score": 4, "verdict_reason": "Strong Python fit."},
            input_hash="stages-report",
            model="test",
            prompt_version="test",
        )
    )
    session.add(
        TailoredCv(
            applicant_id=applicant.id,
            job_id=job.id,
            status=TailoredCvStatus.ready.value,
            input_hash="stages-cv",
            prompt_version="test",
            pdf_content=b"%PDF-1.7\ntailored",
            pdf_size_bytes=18,
        )
    )
    session.flush()
    job_page = await client.get(f"/jobs/{job.id}")
    assert job_page.status_code == 200
    assert "Junior Backend Developer" in job_page.text
    assert "Tailored CV ready" in job_page.text
    assert "Prepare apply" in job_page.text
    assert f'href="/jobs/{job.id}/interview"' in job_page.text

    # Prepare / fill setup
    applications = await client.get("/applications")
    assert applications.status_code == 200
    answers = await client.get("/profile/apply-answers")
    assert answers.status_code == 200
    account = await client.get("/account")
    assert account.status_code == 200
    assert 'id="extension"' in account.text
    prepared = await client.post("/applications", data={"job_id": str(job.id)})
    assert prepared.status_code == 303
    assert prepared.headers["location"].startswith("/applications/")

    # Outreach after applied
    marked = await client.post(f"/jobs/{job.id}/applied")
    assert marked.status_code == 303
    applied_page = await client.get(f"/jobs/{job.id}")
    assert applied_page.status_code == 200
    assert "You applied for this job" in applied_page.text
    outreach = await client.get(f"/jobs/{job.id}/outreach")
    assert outreach.status_code == 200
    assert "Find people" in outreach.text

    # Interview prep
    interview = await client.get(f"/jobs/{job.id}/interview")
    assert interview.status_code == 200
    assert "Interview practice" in interview.text
    assert "Build or refresh practice pack" in interview.text
