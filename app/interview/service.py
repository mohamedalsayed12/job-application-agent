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
from app.interview.plan import fresh_pack, write_pack
from app.interview.roles import role_key as make_role_key
from app.models import (
    ApplicantPreference,
    InterviewIntelRequest,
    InterviewRequestStatus,
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
) -> InterviewIntelRequest:
    """Create or reuse a request for this applicant and role; attach a fresh pack when one exists."""
    now = now or datetime.now(UTC)
    role = role.strip()
    if not role:
        raise ValueError("role is required")
    key = make_role_key(role)
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id))
    allowed = {make_role_key(item) for item in (preference.target_roles if preference else [])}
    if key not in allowed:
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
    if active is not None:
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
    todo = list(
        session.scalars(
            select(InterviewIntelRequest)
            .where(
                InterviewIntelRequest.status.in_(
                    (InterviewRequestStatus.queued.value, InterviewRequestStatus.running.value)
                )
            )
            .order_by(InterviewIntelRequest.created_at)
            .limit(settings.interview_per_cycle)
        )
    )
    for request in todo:
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


def _fail(request: InterviewIntelRequest, error: str, persist: Callable[[], None]) -> None:
    request.status = InterviewRequestStatus.failed.value
    request.error = error[:500]
    persist()
