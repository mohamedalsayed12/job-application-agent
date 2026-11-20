"""The logged-in workflow joins every applicant-facing feature in one place."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import Applicant, ApplicantJobMatch, JobFreshness
from tests.jobs import _job, _li, _store, show_to


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
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


async def test_workflow_requires_login_and_shows_every_stage(client: httpx.AsyncClient) -> None:
    anonymous = await client.get("/workflow", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login"

    registered = await client.post(
        "/register",
        data={"email": "workflow@example.com", "password": "correct horse battery"},
    )
    assert registered.status_code == 303

    response = await client.get("/workflow")
    assert response.status_code == 200
    for heading in (
        "Build your profile from your CV",
        "Find and rank matching jobs",
        "Review fit and tailored CV",
        "Prepare and review the application",
        "Follow up after applying",
        "Prepare for interviews",
    ):
        assert heading in response.text
    assert 'href="/profile/apply-answers"' in response.text
    assert 'href="/workflow" class="active"' in response.text


async def test_workflow_counts_and_links_only_jobs_on_the_listings(client: httpx.AsyncClient, session: Session) -> None:
    await client.post("/register", data={"email": "listed@example.com", "password": "correct horse battery"})
    applicant = session.scalar(select(Applicant).where(Applicant.email == "listed@example.com"))
    _store(
        session,
        [_li(8001, company="Acme"), _li(8002, company="Birch", title="Data Engineer"), _li(8003, company="Cedar", title="Site Engineer")],
    )
    on_listings, elsewhere, closed = (_job(session, n) for n in (8001, 8002, 8003))
    closed.freshness = JobFreshness.closed
    for job, fits in ((on_listings, True), (elsewhere, False), (closed, True)):
        show_to(session, applicant.id, job.id)
        match = session.scalar(
            select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant.id, ApplicantJobMatch.job_id == job.id)
        )
        match.location_fit = fits
    session.flush()

    text = (await client.get("/workflow")).text
    assert "1 current match." in text
    assert f'href="/jobs/{on_listings.id}"' in text
    assert f'href="/jobs/{on_listings.id}/interview"' in text
    assert str(elsewhere.id) not in text
    assert str(closed.id) not in text
