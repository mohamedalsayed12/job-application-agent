"""Single outbound HTTP door: policy, robots, rate limit, size/redirect caps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.fetch.policy import Resolver, check_ssrf, check_url, default_resolve
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
MAX_BODY_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_S = 10.0
# A request may ask for longer, up to this: an Apify run check waits up to 25 s for the run to end.
MAX_TIMEOUT_S = 30.0
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/json",
        "text/json",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
    }
)


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


@dataclass(frozen=True, slots=True)
class _Redirect:
    next_request: FetchRequest


class FetchGateway:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        allowlist: frozenset[str] | None = None,
        resolve: Resolver = default_resolve,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: HostRateLimiter | None = None,
        robots: RobotsCache | None = None,
    ) -> None:
        settings = get_settings()
        self._user_agent = user_agent or settings.fetch_user_agent
        self._allowlist = (
            allowlist if allowlist is not None else settings.api_allowlist_hosts()
        )
        self._resolve = resolve
        self._rate_limiter = rate_limiter or HostRateLimiter()
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": self._user_agent},
        )
        self._robots = robots or RobotsCache(
            user_agent=self._user_agent,
            fetch_body=self._fetch_robots_body,
        )

    async def aclose(self) -> None:
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
            return FetchRefusal(reason=refusal)

        if request.kind == "page":
            allowed = await self._robots.allowed(request.url)
            if not allowed:
                return FetchRefusal(reason="robots")

        host = urlparse(request.url).hostname
        if host is None:
            return FetchRefusal(reason="scheme")

        async with self._rate_limiter.hold(host):
            outcome = await self._send_once(request, host=host)

        if isinstance(outcome, _Redirect):
            if redirects_left <= 0:
                return FetchRefusal(reason="redirect_limit")
            return await self._fetch_hop(
                outcome.next_request,
                redirects_left=redirects_left - 1,
            )
        return outcome

    async def _send_once(
        self,
        request: FetchRequest,
        *,
        host: str,
    ) -> FetchResponse | FetchRefusal | _Redirect:
        headers = {**dict(request.headers), "User-Agent": self._user_agent}
        timeout_s = min(request.timeout_s or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
        try:
            async with self._client.stream(
                request.method,
                request.url,
                headers=headers,
                content=request.body,
                timeout=timeout_s,
            ) as response:
                if response.status_code in {429, 503}:
                    retry_after = _parse_retry_after(response.headers.get("retry-after"))
                    if retry_after is not None:
                        self._rate_limiter.defer(host, min(retry_after, 60.0))

                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return FetchRefusal(reason="redirect_limit")
                    next_url = urljoin(request.url, location)
                    next_host = urlparse(next_url).hostname
                    cross_host = (
                        next_host is not None
                        and next_host.lower().rstrip(".") != host.lower().rstrip(".")
                    )
                    next_headers: Mapping[str, str] = {} if cross_host else request.headers
                    if response.status_code in {307, 308}:
                        next_request = replace(
                            request,
                            url=next_url,
                            headers=next_headers,
                        )
                    else:
                        next_request = replace(
                            request,
                            url=next_url,
                            method="GET",
                            body=None,
                            headers=next_headers,
                        )
                    return _Redirect(next_request=next_request)

                content_type = (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if content_type not in ALLOWED_CONTENT_TYPES:
                    return FetchRefusal(reason="content_type")

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        return FetchRefusal(reason="size")
                    body.extend(chunk)

                return FetchResponse(
                    url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    body=bytes(body),
                    fetched_at=datetime.now(UTC),
                )
        except httpx.TimeoutException:
            return FetchRefusal(reason="timeout")
        except httpx.HTTPError:
            return FetchRefusal(reason="http_error")

    async def _fetch_robots_body(self, robots_url: str) -> str | None:
        """Fetch robots.txt with SSRF + rate limits; only a final 200 body is returned."""
        url = robots_url
        for _ in range(MAX_REDIRECTS + 1):
            if check_ssrf(url, resolve=self._resolve) is not None:
                return None
            host = urlparse(url).hostname
            if host is None:
                return None
            async with self._rate_limiter.hold(host):
                try:
                    async with self._client.stream(
                        "GET",
                        url,
                        headers={"User-Agent": self._user_agent},
                        timeout=DEFAULT_TIMEOUT_S,
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                return None
                            url = urljoin(url, location)
                            continue
                        if response.status_code != 200:
                            return None
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_BODY_BYTES:
                                return None
                            body.extend(chunk)
                        return bytes(body).decode("utf-8", errors="replace")
                except httpx.HTTPError:
                    return None
        return None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(UTC)).total_seconds()
    return delta if delta > 0 else None
