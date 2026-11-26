"""Enqueue and drain interview-intel requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.interview.analyse import analyse_evidence
from app.interview.collect import collect_for_role
from app.interview.job_pack import build_job_pack
from app.interview.plan import fresh_pack, write_pack
from app.interview.roles import role_key as make_role_key
from app.models import (
    ApplicantPreference,
    InterviewIntelRequest,
    InterviewRequestStatus,
    Job,
)


@dataclass(slots=True)
class ProcessReport:
    processed: int = 0
    ready: int = 0
    failed: int = 0
    requeued: int = 0
    stopped_by: str | None = None
    details: list[str] = field(default_factory=list)


def enqueue(
    session: Session,
    *,
    applicant_id: UUID,
    role: str,
    now: datetime | None = None,
    job_id: UUID | None = None,
) -> InterviewIntelRequest:
    """Create or reuse a request for this applicant and role; attach a fresh pack when one exists.

    With `job_id` the role comes from a job the person matched or applied to, so it need not be one of their
    target roles, and that job's practice pack is built as soon as the role research is ready (D77).
    """
    now = now or datetime.now(UTC)
    role = role.strip()
    if not role:
        raise ValueError("role is required")
    key = make_role_key(role)
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id))
    allowed = {make_role_key(item) for item in (preference.target_roles if preference else [])}
    if job_id is None and key not in allowed:
        raise ValueError("role is not one of your target roles")

    active = session.scalar(
        select(InterviewIntelRequest)
        .where(
            InterviewIntelRequest.applicant_id == applicant_id,
            InterviewIntelRequest.role_key == key,
            InterviewIntelRequest.status.in_(
                (InterviewRequestStatus.queued.value, InterviewRequestStatus.running.value)
            ),
        )
        .order_by(InterviewIntelRequest.created_at.desc())
        .limit(1)
    )
    if active is not None and job_id is not None and active.job_id not in (None, job_id):
        # Another job waits on the same research: this job gets its own request, which the worker finishes
        # without researching the role twice (see process_requests).
        active = None
    if active is not None:
        if job_id is not None and active.job_id is None:
            active.job_id = job_id
            session.flush()
        return active

    pack = fresh_pack(session, key, now=now)
    if pack is not None:
        ready = session.scalar(
            select(InterviewIntelRequest)
            .where(
                InterviewIntelRequest.applicant_id == applicant_id,
                InterviewIntelRequest.role_key == key,
                InterviewIntelRequest.status == InterviewRequestStatus.ready.value,
                InterviewIntelRequest.pack_id == pack.id,
            )
            .order_by(InterviewIntelRequest.created_at.desc())
            .limit(1)
        )
        if ready is not None:
            return ready
        request = InterviewIntelRequest(
            applicant_id=applicant_id,
            role=role,
            role_key=key,
            status=InterviewRequestStatus.ready.value,
            pack_id=pack.id,
        )
        session.add(request)
        session.flush()
        return request

    request = InterviewIntelRequest(
        applicant_id=applicant_id,
        role=role,
        role_key=key,
        status=InterviewRequestStatus.queued.value,
        job_id=job_id,
    )
    session.add(request)
    session.flush()
    return request


async def process_requests(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> ProcessReport:
    clock = now or (lambda: datetime.now(UTC))
    report = ProcessReport()
    waiting = list(
        session.scalars(
            select(InterviewIntelRequest)
            .where(
                InterviewIntelRequest.status.in_(
                    (InterviewRequestStatus.queued.value, InterviewRequestStatus.running.value)
                )
            )
            .order_by(InterviewIntelRequest.created_at)
        )
    )
    # The per-cycle limit is for role research. A request whose role is researched already only needs its
    # job's pack, so it is finished now instead of waiting a cycle (D77).
    researching = 0
    researched: set[str] = set()
    for request in waiting:
        if fresh_pack(session, request.role_key, now=clock()) is None:
            # Several requests can wait on one role (one per job): the role is researched once.
            if request.role_key in researched or researching >= settings.interview_per_cycle:
                continue
            researching += 1
            researched.add(request.role_key)
        await _process_one(
            session,
            request,
            gateway=gateway,
            settings=settings,
            persist=persist,
            now=clock,
            sleep=sleep,
            report=report,
        )
    return report


def _halt(request: InterviewIntelRequest, stopped_by: str | None, persist: Callable[[], None], report: ProcessReport) -> bool:
    """Apply a shared stop. Returns True when processing should end for this request."""
    if stopped_by == "budget" or stopped_by == "daily token cap":
        request.status = InterviewRequestStatus.queued.value
        request.error = stopped_by
        persist()
        report.requeued += 1
        report.stopped_by = stopped_by
        return True
    if stopped_by == "not_configured":
        _fail(request, "Provider is not configured", persist)
        report.failed += 1
        return True
    if stopped_by:
        _fail(request, stopped_by, persist)
        report.failed += 1
        return True
    return False


async def _process_one(
    session: Session,
    request: InterviewIntelRequest,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime],
    sleep: Callable[[float], Any] | None,
    report: ProcessReport,
) -> None:
    report.processed += 1
    at = now()
    pack = fresh_pack(session, request.role_key, now=at)
    if pack is not None:
        request.status = InterviewRequestStatus.ready.value
        request.pack_id = pack.id
        request.error = None
        persist()
        report.ready += 1
        await _build_waiting_job_pack(session, request, gateway=gateway, settings=settings, persist=persist)
        return

    request.status = InterviewRequestStatus.running.value
    request.error = None
    persist()

    collect_kwargs: dict[str, Any] = {
        "role": request.role,
        "role_key": request.role_key,
        "gateway": gateway,
        "settings": settings,
        "persist": persist,
        "now": now,
    }
    if sleep is not None:
        collect_kwargs["sleep"] = sleep
    collected = await collect_for_role(session, **collect_kwargs)
    if _halt(request, collected.stopped_by, persist, report):
        return

    analysed = await analyse_evidence(
        session,
        role_key=request.role_key,
        gateway=gateway,
        settings=settings,
        persist=persist,
        now=now(),
    )
    nothing_labelled = analysed.labelled == 0 and analysed.skipped_suspicious == 0
    if (
        analysed.stopped_by
        and (nothing_labelled or analysed.stopped_by in {"budget", "daily token cap", "not_configured"})
        and _halt(request, analysed.stopped_by, persist, report)
    ):
        return

    planned = await write_pack(
        session,
        role=request.role,
        role_key=request.role_key,
        gateway=gateway,
        settings=settings,
        persist=persist,
        now=now(),
    )
    if planned.pack is None:
        reason = planned.stopped_by or "plan failed"
        if planned.grounding_problems and reason == "ungrounded":
            reason = "ungrounded plan"
        if _halt(request, reason, persist, report):
            return
        return

    request.status = InterviewRequestStatus.ready.value
    request.pack_id = planned.pack.id
    request.error = None
    persist()
    report.ready += 1
    await _build_waiting_job_pack(session, request, gateway=gateway, settings=settings, persist=persist)


async def _build_waiting_job_pack(
    session: Session,
    request: InterviewIntelRequest,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
) -> None:
    """The person asked for one job's practice pack and the role research it needs is now ready (D77)."""
    if request.job_id is None:
        return
    job = session.get(Job, request.job_id)
    if job is None:
        request.job_id = None
        persist()
        return
    result = await build_job_pack(
        session, applicant_id=request.applicant_id, job=job, gateway=gateway, settings=settings, role=request.role
    )
    if result.pack is not None:
        request.job_id = None
    else:
        request.error = f"job pack: {result.stopped_by or 'failed'}"[:500]
    persist()


def _fail(request: InterviewIntelRequest, error: str, persist: Callable[[], None]) -> None:
    request.status = InterviewRequestStatus.failed.value
    request.error = error[:500]
    persist()
