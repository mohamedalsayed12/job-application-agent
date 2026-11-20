"""The job page offers interview practice only where it works; reopening shows a queued request."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import (
    Applicant,
    ApplicantJobMatch,
    ApplicantPreference,
    InterviewIntelRequest,
    InterviewRequestStatus,
    SalaryStatus,
)
from tests.jobs import _job, _li, _store

EMAIL = "interview-link@example.com"


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ADMIN_KEY", "test-admin-key-not-default")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    throttle.reset()

    def _db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _signed_in_with_a_job(client: httpx.AsyncClient, session: Session):
    assert (await client.post("/register", data={"email": EMAIL, "password": "correct horse battery"})).status_code == 303
    applicant = session.scalar(select(Applicant).where(Applicant.email == EMAIL))
    _store(session, [_li(7001, title="Backend Developer", content="Build Python APIs.")])
    return applicant, _job(session, 7001)


def _match(session: Session, applicant: Applicant, job) -> None:
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant.id,
            job_id=job.id,
            score=Decimal("4"),
            reasons={"role": {"target": "Backend Developer"}},
            role_match=True,
            location_fit=True,
            salary_status=SalaryStatus.unknown.value,
        )
    )
    session.flush()


async def test_an_unmatched_job_page_and_its_practice_page_are_both_the_unavailable_page(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, job = await _signed_in_with_a_job(client, session)

    page = await client.get(f"/jobs/{job.id}")
    assert page.status_code == 404
    assert "This job isn&#39;t available" in page.text or "This job isn't available" in page.text
    direct = await client.get(f"/jobs/{job.id}/interview")
    assert direct.status_code == 404
    assert direct.headers["content-type"].startswith("text/html")
    assert "This job isn&#39;t available" in direct.text or "This job isn't available" in direct.text


async def test_a_matched_job_links_to_practice_and_shows_a_queued_request(
    client: httpx.AsyncClient, session: Session
) -> None:
    applicant, job = await _signed_in_with_a_job(client, session)
    _match(session, applicant, job)
    assert "Interview practice for this job" in (await client.get(f"/jobs/{job.id}")).text
    assert "queued" not in (await client.get(f"/jobs/{job.id}/interview")).text

    session.add(
        InterviewIntelRequest(
            applicant_id=applicant.id,
            role="Backend Developer",
            role_key="backend developer",
            status=InterviewRequestStatus.queued.value,
        )
    )
    session.flush()

    assert "Role interview research is queued" in (await client.get(f"/jobs/{job.id}/interview")).text


async def test_building_a_pack_for_a_role_no_longer_targeted_shows_not_available(
    client: httpx.AsyncClient, session: Session
) -> None:
    applicant, job = await _signed_in_with_a_job(client, session)
    _match(session, applicant, job)  # the match names a role that is not one of the current target roles

    response = await client.post(f"/jobs/{job.id}/interview")

    assert response.status_code == 404
    assert "available for this job" in response.text


async def test_a_skills_only_match_links_to_practice_for_the_first_target_role(
    client: httpx.AsyncClient, session: Session
) -> None:
    """Pins current behaviour: with no role target on the match, practice uses the person's first target role."""
    applicant, job = await _signed_in_with_a_job(client, session)
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant.id,
            job_id=job.id,
            score=Decimal(4),
            reasons={"role": {"target": ""}},
            role_match=False,
            location_fit=True,
            salary_status=SalaryStatus.unknown.value,
        )
    )
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant.id))
    if preference is None:
        preference = ApplicantPreference(applicant_id=applicant.id, target_roles=[], locations=["London"])
        session.add(preference)
    preference.target_roles = ["Data Engineer", "Backend Developer"]
    session.flush()
    session.add(
        InterviewIntelRequest(
            applicant_id=applicant.id,
            role="Data Engineer",
            role_key="data engineer",
            status=InterviewRequestStatus.queued.value,
        )
    )
    session.flush()

    assert "Interview practice for this job" in (await client.get(f"/jobs/{job.id}")).text
    assert "Role interview research is queued" in (await client.get(f"/jobs/{job.id}/interview")).text
