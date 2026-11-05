"""Health endpoint tests: success when DB is up; 503 when DB is down."""

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health_ok_when_db_reachable(client: httpx.AsyncClient) -> None:
    """Prefer a real Postgres check; skip if DATABASE_URL is unreachable."""
    settings = get_settings()
    try:
        engine = create_engine(settings.database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available for integration check: {exc}")

    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


@pytest.mark.asyncio
async def test_health_unhealthy_when_db_unreachable(client: httpx.AsyncClient) -> None:
    """Force DB failure via mocked engine.connect to keep the test reliable."""
    mock_conn_cm = MagicMock()
    mock_conn_cm.__enter__.side_effect = OperationalError(
        "SELECT 1",
        {},
        Exception("connection refused"),
    )
    mock_conn_cm.__exit__.return_value = False

    with patch("app.api.health.engine") as mock_engine:
        mock_engine.connect.return_value = mock_conn_cm
        response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "unreachable"
