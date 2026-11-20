"""The worker loop survives a failing cycle."""

from __future__ import annotations

import pytest

from app import worker


async def test_a_cycle_that_raises_does_not_stop_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failing_cycle(session: object, gateway: object, *, persist: object = None) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("model download failed")

    async def stop_after_two(_seconds: float) -> None:
        if calls >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(worker, "run_cycle", failing_cycle)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_two)

    with pytest.raises(KeyboardInterrupt):
        await worker._main(once=False, poll_seconds=0)

    assert calls == 2
