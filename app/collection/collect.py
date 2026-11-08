"""Stage 8: read each directory board once a day and keep what came back.

This reads boards and never applicants, so one board is one fetch however many
applicants want it (T27). It never reaches the budget gate, because public board
APIs cost nothing, so it carries on when the budget has run out (T4). Each run is
cleaned in the same transaction that stores it. If cleaning fails, the raw jobs are
kept, and ``python -m app.cleaning`` can replay them.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.boards import BoardFetchResult, fetch_greenhouse_board, fetch_lever_board
from app.boards.lever import LEVER_URLS
from app.cleaning import CleanReport, clean_run
from app.cleaning.freshness import FULL_READS
from app.fetch.gateway import FetchGateway
from app.models import BoardStatus, EmployerBoard, FetchOutcome, FetchRun, RawPayload

COLLECT_EVERY = timedelta(days=1)
RETRY_AFTER = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
Clock = Callable[[], datetime]


@dataclass(slots=True)
class CollectionReport:
    outcomes: Counter[str] = field(default_factory=Counter)
    jobs: int = 0
    failed_boards: list[str] = field(default_factory=list)
    postings_new: int = 0
    closed: int = 0
    reopened: int = 0
    unknown: int = 0
    held: int = 0
    braked_boards: list[str] = field(default_factory=list)
    clean_errors: list[str] = field(default_factory=list)


def is_due(board: EmployerBoard, now: datetime) -> bool:
    if board.last_checked_at is None:
        return True
    failures = board.consecutive_failures
    wait = RETRY_AFTER[failures - 1] if 1 <= failures <= len(RETRY_AFTER) else COLLECT_EVERY
    return now - board.last_checked_at >= wait


async def collect_due(
    session: Session,
    gateway: FetchGateway,
    *,
    clock: Clock | None = None,
    persist: Callable[[], None] | None = None,
) -> CollectionReport:
    """Each board commits alone, so a crash loses at most the board being read."""
    now = clock or (lambda: datetime.now(UTC))
    save = persist or session.flush
    report = CollectionReport()
    # A flagged board is still read: only a complete read can bring its jobs back from unknown.
    board_ids = session.scalars(select(EmployerBoard.id).where(EmployerBoard.status != BoardStatus.retired)).all()
    for board_id in board_ids:
        board = session.scalar(
            select(EmployerBoard).where(EmployerBoard.id == board_id).with_for_update(skip_locked=True)
        )
        if board is None or not is_due(board, now()):
            save()
            continue
        run, detail = await _collect_board(session, gateway, board, now)
        name = f"{board.system}/{board.board_id}"
        _clean(session, run, board, name, now, report)
        save()
        report.outcomes[run.outcome] += 1
        report.jobs += run.jobs_returned
        if run.outcome not in FULL_READS:
            report.failed_boards.append(f"{name}: {run.outcome} ({detail})")
    return report


def _clean(
    session: Session, run: FetchRun, board: EmployerBoard, name: str, now: Clock, report: CollectionReport
) -> None:
    try:
        with session.begin_nested():
            cleaned = clean_run(session, run, board, now=now())
    except Exception as exc:  # A cleaning bug must not lose the fetch or stop the other boards.
        report.clean_errors.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
        return
    _add_cleaning(report, name, cleaned)


def _add_cleaning(report: CollectionReport, name: str, cleaned: CleanReport) -> None:
    report.postings_new += cleaned.postings_new
    report.closed += cleaned.freshness.closed
    report.reopened += cleaned.freshness.reopened
    report.unknown += cleaned.freshness.unknown
    report.held += cleaned.freshness.held
    if cleaned.freshness.brake_on:
        report.braked_boards.append(name)


async def _collect_board(
    session: Session, gateway: FetchGateway, board: EmployerBoard, now: Clock
) -> tuple[FetchRun, str | None]:
    started = now()
    result = await _fetch(gateway, board)
    finished = now()
    label = result.label
    detail = result.detail
    jobs = result.jobs
    run = FetchRun(
        source=board.system,
        employer_board_id=board.id,
        outcome=label,
        http_status=result.status_code,
        jobs_returned=len(jobs),
        request_count=max(1, len(result.attempts)),
        started_at=started,
        finished_at=finished,
    )
    session.add(run)
    session.flush()
    try:
        with session.begin_nested():
            for job in jobs:
                session.add(_raw_payload(run, board.system, job, finished))
            session.flush()
    except Exception as exc:  # One unstorable advert must not stop the other boards being read.
        label = FetchOutcome.failed
        detail = f"storing jobs: {type(exc).__name__}"
        run.outcome = label
        run.jobs_returned = 0
    board.last_checked_at = finished
    if label in FULL_READS:
        board.consecutive_failures = 0
        board.last_job_count = len(jobs)
    else:
        board.consecutive_failures += 1
    if label == FetchOutcome.not_found:
        board.status = BoardStatus.needs_reverify
    session.flush()
    return run, detail


async def _fetch(gateway: FetchGateway, board: EmployerBoard) -> BoardFetchResult:
    try:
        if board.system == "greenhouse":
            return await fetch_greenhouse_board(gateway, board.board_id, with_content=True)
        if board.system == "lever" and board.instance in LEVER_URLS:
            return await fetch_lever_board(gateway, board.board_id, instance=board.instance)
        detail = f"unsupported board: {board.system}/{board.instance}"
    except Exception as exc:  # A bug on one board must not stop the others being read.
        detail = repr(exc)
    return BoardFetchResult(label=FetchOutcome.failed, system=board.system, instance=None, detail=detail)


def _raw_payload(run: FetchRun, source: str, job: dict[str, Any], fetched_at: datetime) -> RawPayload:
    payload = _without_nul(job)
    job_id = payload.get("id")
    return RawPayload(
        fetch_run_id=run.id,
        source=source,
        source_job_id=None if job_id is None else str(job_id),
        payload=payload,
        payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
        fetched_at=fetched_at,
    )


def _without_nul(value: Any) -> Any:
    """Postgres JSONB refuses the NUL character, which real adverts can contain."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_without_nul(item) for item in value]
    if isinstance(value, dict):
        return {_without_nul(key): _without_nul(item) for key, item in value.items()}
    return value
