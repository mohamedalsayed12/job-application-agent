"""Single outbound HTTP door: policy, robots, rate limit, size/redirect caps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx

from app.fetch.policy import (
    DEFAULT_API_ALLOWLIST,
    PolicyRefusal,
    Resolver,
    check_url,
    default_resolve,
)
from app.fetch.rate_limit import HostRateLimiter
from app.fetch.robots import RobotsCache

FetchKind = Literal["api", "page"]
RefusalReason = Literal[
    "blocked_host",
    "robots",
    "private_address",
    "allowlist",
    "size",
    "redirect_limit",
    "timeout",
    "scheme",
    "userinfo",
    "content_type",
    "http_error",
]

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_S = 10.0
ALLOWED_CONTENT_PREFIXES = ("text/html", "application/json", "text/json")


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    method: str = "GET"
    kind: FetchKind = "page"
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    official_domain: str | None = None
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status_code: int
    content_type: str
    body: bytes
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FetchRefusal:
    reason: RefusalReason


class FetchGateway:
    def __init__(
        self,
        *,
        user_agent: str,
        allowlist: frozenset[str] | None = None,
        resolve: Resolver = default_resolve,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: HostRateLimiter | None = None,
        robots: RobotsCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._allowlist = allowlist if allowlist is not None else DEFAULT_API_ALLOWLIST
        self._resolve = resolve
        self._rate_limiter = rate_limiter or HostRateLimiter()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )
        self._robots = robots or RobotsCache(user_agent=user_agent)
        self._robots.set_fetcher(self._fetch_robots_body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, request: FetchRequest) -> FetchResponse | FetchRefusal:
        return await self._fetch_hop(request, redirects_left=MAX_REDIRECTS)

    async def _fetch_hop(
        self,
        request: FetchRequest,
        *,
        redirects_left: int,
    ) -> FetchResponse | FetchRefusal:
        refusal = check_url(
            request.url,
            kind=request.kind,
            official_domain=request.official_domain,
            allowlist=self._allowlist,
            resolve=self._resolve,
        )
        if refusal is not None:
            return FetchRefusal(reason=_as_reason(refusal.reason))

        if request.kind == "page":
            try:
                allowed = await self._robots.allowed(request.url)
            except Exception:
                allowed = True
            if not allowed:
                return FetchRefusal(reason="robots")

        host = urlparse(request.url).hostname
        if host is None:
            return FetchRefusal(reason="scheme")

        await self._rate_limiter.acquire(host)
        try:
            return await self._send(request, redirects_left=redirects_left)
        finally:
            self._rate_limiter.release(host)

    async def _send(
        self,
        request: FetchRequest,
        *,
        redirects_left: int,
    ) -> FetchResponse | FetchRefusal:
        headers = {"User-Agent": self._user_agent, **dict(request.headers)}
        try:
            response = await self._client.request(
                request.method,
                request.url,
                headers=headers,
                content=request.body,
                timeout=request.timeout_s,
            )
        except httpx.TimeoutException:
            return FetchRefusal(reason="timeout")
        except httpx.HTTPError:
            return FetchRefusal(reason="http_error")

        if response.status_code in {301, 302, 303, 307, 308}:
            if redirects_left <= 0:
                return FetchRefusal(reason="redirect_limit")
            location = response.headers.get("location")
            if not location:
                return FetchRefusal(reason="redirect_limit")
            next_url = urljoin(request.url, location)
            next_request = FetchRequest(
                url=next_url,
                method=request.method if response.status_code in {307, 308} else "GET",
                kind=request.kind,
                headers=request.headers,
                timeout_s=request.timeout_s,
                official_domain=request.official_domain,
            )
            return await self._fetch_hop(
                next_request,
                redirects_left=redirects_left - 1,
            )

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not _allowed_content_type(content_type):
            return FetchRefusal(reason="content_type")

        body = response.content
        if len(body) > MAX_BODY_BYTES:
            return FetchRefusal(reason="size")

        return FetchResponse(
            url=str(response.url),
            status_code=response.status_code,
            content_type=content_type,
            body=body[:MAX_BODY_BYTES],
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_robots_body(self, robots_url: str) -> str | None:
        """Fetch robots.txt with SSRF + rate limits, skipping robots recursion."""
        refusal = check_url(
            robots_url,
            kind="page",
            official_domain=urlparse(robots_url).hostname,
            allowlist=self._allowlist,
            resolve=self._resolve,
        )
        if refusal is not None:
            return None
        host = urlparse(robots_url).hostname
        if host is None:
            return None
        await self._rate_limiter.acquire(host)
        try:
            try:
                response = await self._client.get(
                    robots_url,
                    headers={"User-Agent": self._user_agent},
                    timeout=DEFAULT_TIMEOUT_S,
                )
            except httpx.HTTPError:
                return None
            if response.status_code >= 400:
                return None
            return response.text
        finally:
            self._rate_limiter.release(host)


def _allowed_content_type(content_type: str) -> bool:
    return any(
        content_type == prefix or content_type.startswith(prefix)
        for prefix in ALLOWED_CONTENT_PREFIXES
    )


def _as_reason(reason: str) -> RefusalReason:
    allowed: Sequence[str] = (
        "blocked_host",
        "robots",
        "private_address",
        "allowlist",
        "size",
        "redirect_limit",
        "timeout",
        "scheme",
        "userinfo",
        "content_type",
        "http_error",
    )
    if reason in allowed:
        return reason  # type: ignore[return-value]
    return "http_error"


def build_default_gateway(
    *,
    user_agent: str,
    allowlist: frozenset[str] | None = None,
    **kwargs: Any,
) -> FetchGateway:
    return FetchGateway(user_agent=user_agent, allowlist=allowlist, **kwargs)
