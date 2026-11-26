"""The logged-in workflow joins every applicant-facing feature in one place."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from tests.auth import sign_up


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

    registered = await sign_up(client, "workflow@example.com", "correct horse battery")
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
