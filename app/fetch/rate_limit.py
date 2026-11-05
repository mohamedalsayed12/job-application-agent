"""In-process per-host spacing: one in flight, at least 1s between starts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


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
        self._last_start: dict[str, float] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        key = host.lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, host: str) -> None:
        key = host.lower()
        lock = self._lock_for(key)
        await lock.acquire()
        last = self._last_start.get(key)
        now = self._clock()
        if last is not None:
            wait = self._min_interval_s - (now - last)
            if wait > 0:
                await self._sleeper(wait)
        self._last_start[key] = self._clock()

    def release(self, host: str) -> None:
        lock = self._locks.get(host.lower())
        if lock is not None and lock.locked():
            lock.release()
