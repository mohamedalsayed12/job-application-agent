"""Health endpoint tests: success when DB is up; 503 when DB is down."""

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app.core.db import CONNECT_TIMEOUT_SECONDS, make_engine
from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_engine")
async def test_health_ok_when_db_reachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_engine")
async def test_health_head_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.head("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_unhealthy_when_db_unreachable(client: httpx.AsyncClient) -> None:
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
    assert "detail" not in body
    assert "connection refused" not in response.text


@pytest.mark.asyncio
async def test_health_returns_503_promptly_when_db_never_answers(
    client: httpx.AsyncClient,
) -> None:
    # Accepts TCP and stays silent, like Docker's port relay while Postgres starts.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    port = silent.getsockname()[1]
    engine = make_engine(f"postgresql+psycopg://u:p@127.0.0.1:{port}/db")
    try:
        with patch("app.api.health.engine", engine):
            started = time.monotonic()
            response = await asyncio.wait_for(
                client.get("/health"), timeout=CONNECT_TIMEOUT_SECONDS + 10
            )
    finally:
        engine.dispose()
        silent.close()

    assert response.status_code == 503
    assert time.monotonic() - started < CONNECT_TIMEOUT_SECONDS + 5
