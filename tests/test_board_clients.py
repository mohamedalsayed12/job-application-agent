"""T18 + every FetchOutcome label for Greenhouse and Lever board clients."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest

from app.boards import fetch_greenhouse_board, fetch_lever_board
from app.fetch.gateway import FetchGateway
from app.models import FetchOutcome

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "boards"

ALLOWLIST = frozenset(
    {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.eu.lever.co",
    }
)


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _gateway(
    handler: Callable[[httpx.Request], httpx.Response],
) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=ALLOWLIST,
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),
    )


def _json_response(status: int, body: bytes) -> httpx.Response:
    return httpx.Response(
        status,
        content=body,
        headers={"content-type": "application/json"},
    )


# --- Greenhouse labels ---


@pytest.mark.asyncio
async def test_greenhouse_complete() -> None:
    body = _fixture("greenhouse_complete_example.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "boards-api.greenhouse.io"
        assert request.url.path == "/v1/boards/example/jobs"
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "example")
    await gateway.aclose()
    assert result.label == FetchOutcome.complete
    assert result.system == "greenhouse"
    assert len(result.jobs) == 2
    assert "updated_at" in result.jobs[0]


@pytest.mark.asyncio
async def test_greenhouse_empty() -> None:
    body = _fixture("greenhouse_empty.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "empty-board")
    await gateway.aclose()
    assert result.label == FetchOutcome.empty
    assert result.jobs == ()


@pytest.mark.asyncio
async def test_greenhouse_not_found() -> None:
    body = _fixture("greenhouse_not_found.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(404, body)

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "missing-token")
    await gateway.aclose()
    assert result.label == FetchOutcome.not_found
    assert result.detail == "Job not found"


@pytest.mark.asyncio
async def test_greenhouse_partial_truncated() -> None:
    body = _fixture("greenhouse_partial_truncated.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "example")
    await gateway.aclose()
    assert result.label == FetchOutcome.partial
    assert result.detail == "unparseable_json"


@pytest.mark.asyncio
async def test_greenhouse_partial_meta_mismatch() -> None:
    body = _fixture("greenhouse_partial_meta_mismatch.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "example")
    await gateway.aclose()
    assert result.label == FetchOutcome.partial
    assert result.detail == "meta_total_mismatch"
    assert len(result.jobs) == 2


@pytest.mark.asyncio
async def test_greenhouse_failed_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(503, b'{"error":"unavailable"}')

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "example")
    await gateway.aclose()
    assert result.label == FetchOutcome.failed
    assert result.detail == "http_503"


@pytest.mark.asyncio
async def test_greenhouse_failed_gateway_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "example")
    await gateway.aclose()
    assert result.label == FetchOutcome.failed
    assert result.detail == "timeout"


@pytest.mark.asyncio
async def test_greenhouse_invalid_board_id_no_network() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _json_response(200, b'{"jobs":[],"meta":{"total":0}}')

    gateway = _gateway(handler)
    result = await fetch_greenhouse_board(gateway, "../evil")
    await gateway.aclose()
    assert result.label == FetchOutcome.failed
    assert result.detail == "invalid_board_id"
    assert seen == []


# --- Lever labels + T18 ---


@pytest.mark.asyncio
async def test_lever_complete_global() -> None:
    body = _fixture("lever_global_complete_leverdemo.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.lever.co"
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "leverdemo")
    await gateway.aclose()
    assert result.label == FetchOutcome.complete
    assert result.instance == "lever_global"
    assert result.attempts == ("lever_global",)
    assert len(result.jobs) == 2
    assert "createdAt" in result.jobs[0]


@pytest.mark.asyncio
async def test_lever_empty_global_does_not_fall_through() -> None:
    """Empty [] on global is a real empty board on that instance, not T18."""
    body = _fixture("lever_global_empty_lever.json")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "lever")
    await gateway.aclose()
    assert result.label == FetchOutcome.empty
    assert result.instance == "lever_global"
    assert seen == ["api.lever.co"]


@pytest.mark.asyncio
async def test_t18_lever_not_found_global_then_eu_complete() -> None:
    """T18: global 404 → EU checked before any decision."""
    not_found = _fixture("lever_not_found.json")
    eu_body = _fixture("lever_eu_complete_lever.json")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        seen.append(host)
        if host == "api.lever.co":
            return _json_response(404, not_found)
        if host == "api.eu.lever.co":
            return _json_response(200, eu_body)
        return _json_response(500, b"unexpected")

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "lever")
    await gateway.aclose()
    assert result.label == FetchOutcome.complete
    assert result.instance == "lever_eu"
    assert result.attempts == ("lever_global", "lever_eu")
    assert seen == ["api.lever.co", "api.eu.lever.co"]
    assert len(result.jobs) == 2


@pytest.mark.asyncio
async def test_lever_not_found_both_instances() -> None:
    not_found = _fixture("lever_not_found.json")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host or "")
        return _json_response(404, not_found)

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "missing-site")
    await gateway.aclose()
    assert result.label == FetchOutcome.not_found
    assert result.attempts == ("lever_global", "lever_eu")
    assert seen == ["api.lever.co", "api.eu.lever.co"]
    assert result.detail == "Document not found"


@pytest.mark.asyncio
async def test_lever_partial_truncated() -> None:
    body = _fixture("lever_partial_truncated.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, body)

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "leverdemo")
    await gateway.aclose()
    assert result.label == FetchOutcome.partial
    assert result.detail == "unparseable_json"


@pytest.mark.asyncio
async def test_lever_failed_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b'{"ok":false,"error":"rate"}',
            headers={"content-type": "application/json", "retry-after": "2"},
        )

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "leverdemo")
    await gateway.aclose()
    assert result.label == FetchOutcome.failed
    assert result.detail == "http_429"
    assert result.attempts == ("lever_global",)


@pytest.mark.asyncio
async def test_lever_eu_allowlisted() -> None:
    """api.eu.lever.co must be on the allowlist (not eu.lever.co)."""
    eu_body = _fixture("lever_eu_complete_lever.json")
    not_found = _fixture("lever_not_found.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return _json_response(404, not_found)
        return _json_response(200, eu_body)

    gateway = _gateway(handler)
    result = await fetch_lever_board(gateway, "lever")
    await gateway.aclose()
    assert result.label == FetchOutcome.complete
    assert result.url is not None
    assert "api.eu.lever.co" in result.url
