"""In-process per-host spacing: one in flight, at least 1s between starts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class HostRateLimiter:
    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleeper = sleeper
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed_at: dict[str, float] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        key = host.lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def hold(self, host: str) -> AsyncIterator[None]:
        key = host.lower()
        lock = self._lock_for(key)
        await lock.acquire()
        try:
            now = self._clock()
            wait = self._next_allowed_at.get(key, now) - now
            if wait > 0:
                await self._sleeper(wait)
            self._next_allowed_at[key] = self._clock() + self._min_interval_s
            yield
        finally:
            lock.release()

    def defer(self, host: str, seconds: float) -> None:
        key = host.lower()
        earliest = self._clock() + max(0.0, seconds)
        previous = self._next_allowed_at.get(key, 0.0)
        self._next_allowed_at[key] = max(previous, earliest)
