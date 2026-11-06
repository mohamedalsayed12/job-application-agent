"""Run Stage 5 verification on a claimed candidate and persist outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway
from app.models import (
    CANDIDATE_LIVE_UNIQUE_EXCLUDES,
    Candidate,
    CandidateState,
    EvidenceLevel,
    ReviewItem,
    VerificationCheck,
    VerificationOutcome,
)
from app.queue.retry import schedule_verification_failure
from app.verify import api as api_mod
from app.verify import careers as careers_mod
from app.verify import decide
from app.verify import domain as domain_mod
from app.verify import ownership as ownership_mod
from app.verify.types import CheckRecord, VerifyDecision


async def verify_candidate(
    session: Session,
    candidate: Candidate,
    gateway: FetchGateway,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> VerifyDecision:
    """Verify a ``verifying`` candidate. Mutates the row; caller commits."""
    if candidate.state != CandidateState.verifying:
        raise ValueError(
            f"candidate {candidate.id} must be verifying, got {candidate.state}"
        )
    stamp = now or (clock() if clock else datetime.now(UTC))
    decision = await _run_checks(candidate, gateway, now=stamp)
    return _persist(session, candidate, decision, now=stamp)


async def _run_checks(
    candidate: Candidate,
    gateway: FetchGateway,
    *,
    now: datetime,
) -> VerifyDecision:
    domain = await domain_mod.confirm_official_domain(gateway, candidate)
    if domain.record.outcome == VerificationOutcome.error:
        return decide.retry(
            domain.record.evidence_snippet or "official_domain_error",
            (domain.record,),
        )
    if not domain.confirmed or domain.domain is None:
        return decide.needs_review(
            reason=domain.record.evidence_snippet or "official_domain_unconfirmed",
            checks=(domain.record,),
            priority=40,
        )

    board_system = candidate.board_system
    if board_system not in {"greenhouse", "lever"} or not candidate.board_id:
        unsupported = await careers_mod.find_unsupported_careers(
            gateway,
            domain=domain.domain,
            homepage_html=domain.homepage_html,
            homepage_url=domain.homepage_url,
        )
        if unsupported == "retry":
            return decide.retry("careers_fetch_error", (domain.record,))
        if unsupported is not None:
            return decide.unsupported(domain=domain, careers=unsupported, now=now)
        return decide.needs_review(
            reason="no_supported_public_board",
            checks=(domain.record,),
            evidence_level=EvidenceLevel.weak,
            priority=45,
            confirmed_domain=domain.domain,
        )

    api = await api_mod.check_api_access(
        gateway,
        system=board_system,
        board_id=candidate.board_id,
    )
    if api.access == "failed":
        return decide.retry(
            api.failure_detail or "api_failed",
            (domain.record, api.record),
        )

    ownership = await ownership_mod.assess_ownership(
        gateway,
        candidate,
        domain=domain.domain,
        homepage_html=domain.homepage_html,
        homepage_url=domain.homepage_url,
        api_company_names=api.company_names,
        api_job_ids=api.job_ids,
    )
    if ownership == "retry":
        return decide.retry(
            "ownership_fetch_error",
            (domain.record, api.record),
        )

    return decide.from_board_checks(
        domain=domain,
        ownership=ownership,
        api=api,
    )


def _persist(
    session: Session,
    candidate: Candidate,
    decision: VerifyDecision,
    *,
    now: datetime,
) -> VerifyDecision:
    for record in decision.checks:
        _add_check(session, candidate.id, record, now=now)

    if decision.state == "retry_wait":
        plan = schedule_verification_failure(
            candidate,
            error=decision.reason,
            now=now,
        )
        if plan.state == CandidateState.needs_review:
            decision = replace(
                decision,
                state="needs_review",
                review_priority=20,
            )
            _open_review(session, candidate, decision)
        session.flush()
        return decision

    if (
        decision.confirmed_domain
        and _domain_taken(session, candidate, decision.confirmed_domain)
    ):
        decision = replace(
            decision,
            state="needs_review",
            reason="official_domain_conflict",
            review_priority=15,
            board_instance=None,
        )

    candidate.claimed_at = None
    candidate.next_attempt_at = None
    candidate.last_error = None if decision.state == "verified" else decision.reason

    # Only rewrite Lever instance after a confirmed API read (not not_found).
    if (
        decision.state == "verified"
        and decision.board_instance
        and candidate.board_system == "lever"
    ):
        candidate.board_instance = decision.board_instance

    if decision.confirmed_domain and decision.state != "needs_review":
        candidate.primary_domain = decision.confirmed_domain
    elif (
        decision.confirmed_domain
        and decision.state == "needs_review"
        and decision.reason != "official_domain_conflict"
        and not _domain_taken(session, candidate, decision.confirmed_domain)
    ):
        candidate.primary_domain = decision.confirmed_domain

    if decision.state == "verified":
        candidate.state = CandidateState.verified
    elif decision.state == "unsupported":
        candidate.state = CandidateState.unsupported
        candidate.revisit_at = decision.revisit_at
        if decision.confirmed_domain:
            candidate.primary_domain = decision.confirmed_domain
    else:
        candidate.state = CandidateState.needs_review
        _open_review(session, candidate, decision)

    session.flush()
    return decision


def _domain_taken(session: Session, candidate: Candidate, domain: str) -> bool:
    other = session.scalar(
        select(Candidate.id).where(
            Candidate.primary_domain == domain,
            Candidate.id != candidate.id,
            Candidate.state.not_in(tuple(CANDIDATE_LIVE_UNIQUE_EXCLUDES)),
        )
    )
    return other is not None


def _add_check(
    session: Session,
    candidate_id,
    record: CheckRecord,
    *,
    now: datetime,
) -> None:
    session.add(
        VerificationCheck(
            candidate_id=candidate_id,
            check_type=record.check_type,
            outcome=record.outcome,
            evidence_url=record.evidence_url,
            evidence_snippet=record.evidence_snippet,
            http_status=record.http_status,
            checked_at=now,
        )
    )


def _open_review(
    session: Session,
    candidate: Candidate,
    decision: VerifyDecision,
) -> None:
    existing = session.scalar(
        select(ReviewItem).where(
            ReviewItem.candidate_id == candidate.id,
            ReviewItem.decided_at.is_(None),
        )
    )
    if existing is not None:
        existing.reason = decision.reason
        existing.priority = decision.review_priority
        return
    session.add(
        ReviewItem(
            candidate_id=candidate.id,
            reason=decision.reason,
            priority=decision.review_priority,
        )
    )
