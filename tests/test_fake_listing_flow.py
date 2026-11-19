"""D55 end to end: the worker's fake-listing check takes a job off listings and every /jobs/{id} route."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.orm import Session

from app import worker
from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import Job, JobVerification, ListingVerdict
from app.worker import run_cycle
from tests.jobs import _job, _li, _store
from tests.test_listings import ADVERT, PASSWORD, _titles

CH_HOST = "api.company-information.service.gov.uk"


def _register(session: Session) -> None:
    _store(
        session,
        [
            _li(1, title="Junior Backend Developer", company="Gone Ltd", content=f"{ADVERT} Advert 1."),
            _li(2, title="Graduate Backend Developer", company="Acme", content=f"{ADVERT} Advert 2."),
        ],
    )
    session.commit()


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == CH_HOST:
        name = request.url.params["q"]
        status = "dissolved" if name == "Gone Ltd" else "active"
        return httpx.Response(200, json={"items": [{"company_number": "1", "title": name, "company_status": status}]})
    return httpx.Response(404)


def _gateway() -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({CH_HOST}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(_handler),
    )


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    async def _override_gateway() -> AsyncIterator[FetchGateway]:
        gateway = _gateway()
        yield gateway
        await gateway.aclose()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[get_gateway] = _override_gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        assert (await test_client.post("/register", data={"email": "f@example.com", "password": PASSWORD})).status_code == 303
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _save_profile(client: httpx.AsyncClient) -> None:
    profile = {
        "target_roles": "Junior Backend Developer, Graduate Backend Developer",
        "locations": "Manchester",
        "experience_level": "entry",
        "skills": "Python",
    }
    assert (await client.post("/profile", data=profile)).status_code == 303


async def _job_routes(client: httpx.AsyncClient, job: Job) -> dict[str, int]:
    base = f"/jobs/{job.id}"
    return {
        "page": (await client.get(base)).status_code,
        "tailor": (await client.post(f"{base}/tailor")).status_code,
        "tailored.pdf": (await client.get(f"{base}/tailored.pdf")).status_code,
        "applied": (await client.post(f"{base}/applied")).status_code,
        "interview": (await client.get(f"{base}/interview")).status_code,
        "outreach": (await client.get(f"{base}/outreach")).status_code,
    }


async def test_a_dissolved_employer_leaves_the_listings_after_one_worker_cycle(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(session)
    await _save_profile(client)
    before = _titles((await client.get("/listings")).text)
    assert "Junior Backend Developer" in before

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: Settings(apify_api_token="", companies_house_api_key="test-ch-key", api_allowlist=CH_HOST),
    )
    gateway = _gateway()
    await run_cycle(session, gateway)
    await gateway.aclose()
    session.commit()

    after = _titles((await client.get("/listings")).text)
    assert "Junior Backend Developer" not in after
    assert "Graduate Backend Developer" in after
    assert set((await _job_routes(client, _job(session, 1))).values()) == {404}
    assert (await client.get(f"/jobs/{_job(session, 2).id}")).status_code == 200


async def test_a_job_judged_fake_is_unreachable_even_before_the_rematch(
    client: httpx.AsyncClient, session: Session
) -> None:
    _register(session)
    await _save_profile(client)
    job = _job(session, 1)
    assert (await client.get(f"/jobs/{job.id}")).status_code == 200
    session.add(
        JobVerification(
            job_id=job.id,
            source_hash="",
            outcome=ListingVerdict.fake,
            reasons=["company not trading on Companies House"],
            checked_at=datetime.now(UTC),
        )
    )
    session.commit()

    assert set((await _job_routes(client, job)).values()) == {404}
