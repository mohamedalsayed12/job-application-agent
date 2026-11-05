"""T19 / T20 / T21: fetch gateway safety with mocked HTTP and DNS."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Sequence

import httpx
import pytest

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.fetch.rate_limit import HostRateLimiter


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _private_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("127.0.0.1")]


def _gateway(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    resolve: Callable[[str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]] = _public_resolve,
    rate_limiter: HostRateLimiter | None = None,
) -> FetchGateway:
    transport = httpx.MockTransport(handler)
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.example.com", "boards-api.greenhouse.io"}),
        resolve=resolve,
        transport=transport,
        rate_limiter=rate_limiter,
    )


@pytest.mark.asyncio
async def test_t19_http_scheme_refused() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, text="nope"))
    result = await gateway.fetch(
        FetchRequest(url="http://api.example.com/v1", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "scheme"
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t19_ip_literal_host_refused() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    gateway = _gateway(handler)
    result = await gateway.fetch(
        FetchRequest(url="https://127.0.0.1/secret", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "private_address"
    assert seen == []
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t19_private_dns_refused() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    gateway = _gateway(handler, resolve=_private_resolve)
    result = await gateway.fetch(
        FetchRequest(url="https://api.example.com/v1", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "private_address"
    assert seen == []
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t19_redirect_to_private_never_requested() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"Location": "https://api.example.com/admin"},
                json={},
            )
        return httpx.Response(200, json={"leaked": True})

    def resolve(host: str) -> list[ipaddress.IPv4Address]:
        # First hop resolves public; after redirect, same name rebinds private.
        if seen and seen[-1].endswith("/start"):
            return [ipaddress.IPv4Address("10.0.0.5")]
        return [ipaddress.IPv4Address("93.184.216.34")]

    gateway = _gateway(handler, resolve=resolve)
    result = await gateway.fetch(
        FetchRequest(url="https://api.example.com/start", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "private_address"
    assert seen == ["https://api.example.com/start"]
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t20_robots_disallow_blocks_page() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private\n",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            200,
            text="<html>secret</html>",
            headers={"content-type": "text/html"},
        )

    gateway = _gateway(handler)
    result = await gateway.fetch(
        FetchRequest(
            url="https://careers.example.org/private",
            kind="page",
            official_domain="example.org",
        ),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "robots"
    assert "https://careers.example.org/private" not in seen
    assert any(url.endswith("/robots.txt") for url in seen)
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t20_blocked_hosts_refused_before_connect() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="nope", headers={"content-type": "text/html"})

    gateway = _gateway(handler)
    for url in (
        "https://www.linkedin.com/jobs/1",
        "https://indeed.com/viewjob",
        "https://www.glassdoor.com/Job/1.htm",
    ):
        result = await gateway.fetch(
            FetchRequest(url=url, kind="page", official_domain="linkedin.com"),
        )
        assert isinstance(result, FetchRefusal)
        assert result.reason == "blocked_host"
    assert seen == []
    await gateway.aclose()


@pytest.mark.asyncio
async def test_t21_same_host_requests_spaced_one_second() -> None:
    clock = {"now": 1000.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    limiter = HostRateLimiter(
        min_interval_s=1.0,
        clock=lambda: clock["now"],
        sleeper=fake_sleep,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    gateway = _gateway(handler, rate_limiter=limiter)
    first = await gateway.fetch(FetchRequest(url="https://api.example.com/a", kind="api"))
    second = await gateway.fetch(FetchRequest(url="https://api.example.com/b", kind="api"))
    assert isinstance(first, FetchResponse)
    assert isinstance(second, FetchResponse)
    assert sleeps == [1.0]
    await gateway.aclose()


@pytest.mark.asyncio
async def test_oversized_body_refused_without_buffering_all() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (2 * 1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

    gateway = _gateway(handler)
    result = await gateway.fetch(
        FetchRequest(url="https://api.example.com/big", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "size"
    await gateway.aclose()


@pytest.mark.asyncio
async def test_same_host_redirect_does_not_deadlock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"Location": "https://api.example.com/done"},
            )
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"content-type": "application/json"},
        )

    clock = {"now": 0.0}

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    limiter = HostRateLimiter(
        min_interval_s=1.0,
        clock=lambda: clock["now"],
        sleeper=fake_sleep,
    )
    gateway = _gateway(handler, rate_limiter=limiter)
    result = await gateway.fetch(
        FetchRequest(url="https://api.example.com/start", kind="api"),
    )
    assert isinstance(result, FetchResponse)
    assert result.status_code == 200
    await gateway.aclose()


@pytest.mark.asyncio
async def test_allowlist_rejects_unknown_api_host() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json={}))
    result = await gateway.fetch(
        FetchRequest(url="https://evil-api.example.net/v1", kind="api"),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "allowlist"
    await gateway.aclose()


@pytest.mark.asyncio
async def test_page_off_official_domain_refused() -> None:
    gateway = _gateway(
        lambda request: httpx.Response(200, text="<html/>", headers={"content-type": "text/html"})
    )
    result = await gateway.fetch(
        FetchRequest(
            url="https://other.example.net/about",
            kind="page",
            official_domain="example.org",
        ),
    )
    assert isinstance(result, FetchRefusal)
    assert result.reason == "allowlist"
    await gateway.aclose()
