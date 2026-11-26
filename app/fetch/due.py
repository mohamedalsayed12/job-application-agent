"""Which searches are due (D72). Each role and location is due on its own, so a title someone adds
today is searched on the worker's next cycle, not when the whole source next runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import FetchOutcome, FetchRun


class _Search(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def location(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Due[S: _Search]:
    search: S
    # Never searched successfully before: look further back, because nothing has been collected for it yet.
    first: bool

    @property
    def key(self) -> str:
        return search_key(self.search)


def search_key(search: _Search) -> str:
    return f"{' '.join(search.role.lower().split())}|{' '.join(search.location.lower().split())}"


def due_searches[S: _Search](
    session: Session, source: str, searches: Iterable[S], *, now: datetime, every_hours: int, settings: Settings
) -> list[Due[S]]:
    """The searches to run now, in the order given.

    A search that worked waits `every_hours`. One that failed is tried again after
    `search_retry_hours`, so a broken key or an outage is not retried every cycle.
    """
    wanted = list(searches)
    keys = {search_key(search) for search in wanted}
    if not keys:
        return []
    worked = func.max(FetchRun.started_at).filter(FetchRun.outcome != FetchOutcome.failed.value)
    last = {
        key: (any_run, good_run)
        for key, any_run, good_run in session.execute(
            select(FetchRun.search_key, func.max(FetchRun.started_at), worked)
            .where(FetchRun.source == source, FetchRun.search_key.in_(keys))
            .group_by(FetchRun.search_key)
        )
    }
    due: list[Due[S]] = []
    for search in wanted:
        any_run, good_run = last.get(search_key(search), (None, None))
        if good_run is not None and now - good_run < timedelta(hours=every_hours):
            continue
        if any_run is not None and any_run != good_run and now - any_run < timedelta(hours=settings.search_retry_hours):
            continue
        due.append(Due(search, first=good_run is None))
    return due


def never_searched(session: Session, searches: Iterable[_Search]) -> int:
    """How many of these searches no source has run yet, for telling someone their jobs are on the way."""
    keys = {search_key(search) for search in searches}
    if not keys:
        return 0
    done = set(session.scalars(select(FetchRun.search_key).where(FetchRun.search_key.in_(keys)).distinct()))
    return len(keys - done)


def failed_run(session: Session, source: str, key: str, *, started_at: datetime, finished_at: datetime) -> None:
    """A search that never started. Recorded so it is retried after `search_retry_hours`, not every cycle."""
    session.add(
        FetchRun(
            source=source,
            outcome=FetchOutcome.failed.value,
            jobs_returned=0,
            request_count=1,
            started_at=started_at,
            finished_at=finished_at,
            search_key=key,
        )
    )
    session.flush()
