"""robots.txt checks with a 24-hour in-process cache (page fetches only)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

FetchRobots = Callable[[str], Awaitable[str | None]]


@dataclass
class _CacheEntry:
    parser: RobotFileParser | None
    expires_at: float
    # None parser means "transport failed once; retry next time" — not cached long.


class RobotsCache:
    def __init__(
        self,
        *,
        ttl_s: float = 24 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
        user_agent: str,
        fetch_body: FetchRobots | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._user_agent = user_agent
        self._fetch_body = fetch_body
        self._cache: dict[str, _CacheEntry] = {}

    def set_fetcher(self, fetch_body: FetchRobots) -> None:
        self._fetch_body = fetch_body

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
        now = self._clock()
        entry = self._cache.get(origin)
        if entry is not None and entry.expires_at > now and entry.parser is not None:
            return entry.parser.can_fetch(self._user_agent, url)

        if self._fetch_body is None:
            raise RuntimeError("robots fetcher not configured")

        body = await self._fetch_body(f"{origin}/robots.txt")
        if body is None:
            # shortcut: fail-open on robots transport errors until audit evidence
            # needs fail-closed; do not cache a long allow on failure.
            logger.warning("robots.txt fetch failed for %s; allowing once", origin)
            return True

        parser = RobotFileParser()
        parser.parse(body.splitlines())
        self._cache[origin] = _CacheEntry(parser=parser, expires_at=now + self._ttl_s)
        return parser.can_fetch(self._user_agent, url)
