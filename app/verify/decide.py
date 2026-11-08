"""Stage 5 step 4: compose verified / needs_review / unsupported (never rejected)."""

from __future__ import annotations

from datetime import datetime

from app.models import EvidenceLevel
from app.verify.types import (
    UNSUPPORTED_REVISIT,
    ApiAccessCheck,
    CheckRecord,
    OfficialDomainCheck,
    OwnershipAssessment,
    UnsupportedCareers,
    VerifyDecision,
)


def from_board_checks(
    *,
    domain: OfficialDomainCheck,
    ownership: OwnershipAssessment,
    api: ApiAccessCheck,
) -> VerifyDecision:
    checks = (domain.record, ownership.record, api.record)
    confirmed = domain.domain if domain.confirmed else None
    if (
        domain.confirmed
        and ownership.level == EvidenceLevel.strong
        and api.access == "confirmed"
    ):
        return VerifyDecision(
            state="verified",
            reason="official_domain_strong_evidence_api_ok",
            checks=checks,
            evidence_level=EvidenceLevel.strong,
            board_instance=api.instance,
            last_job_count=api.job_count,
            confirmed_domain=confirmed,
        )
    if api.access == "not_found":
        return VerifyDecision(
            state="needs_review",
            reason="api_not_found",
            checks=checks,
            evidence_level=ownership.level,
            confirmed_domain=confirmed,
            review_priority=40,
        )
    return VerifyDecision(
        state="needs_review",
        reason=f"ownership_{ownership.level.value}",
        checks=checks,
        evidence_level=ownership.level,
        last_job_count=api.job_count,
        confirmed_domain=confirmed,
        review_priority=30 if ownership.level == EvidenceLevel.medium else 50,
    )


def unsupported(
    *,
    domain: OfficialDomainCheck,
    careers: UnsupportedCareers,
    now: datetime,
) -> VerifyDecision:
    return VerifyDecision(
        state="unsupported",
        reason=f"unsupported_careers:{careers.system}",
        checks=(domain.record, careers.record),
        careers_system=careers.system,
        revisit_at=now + UNSUPPORTED_REVISIT,
        confirmed_domain=domain.domain if domain.confirmed else None,
        review_priority=10,
    )


def recruitment_agency(*, domain: OfficialDomainCheck, now: datetime) -> VerifyDecision:
    return VerifyDecision(
        state="unsupported",
        reason="recruitment_agency",
        checks=(domain.record,),
        revisit_at=now + UNSUPPORTED_REVISIT,
        confirmed_domain=domain.domain if domain.confirmed else None,
        review_priority=10,
    )


def needs_review(
    *,
    reason: str,
    checks: tuple[CheckRecord, ...],
    evidence_level: EvidenceLevel | None = None,
    priority: int = 50,
    confirmed_domain: str | None = None,
) -> VerifyDecision:
    return VerifyDecision(
        state="needs_review",
        reason=reason,
        checks=checks,
        evidence_level=evidence_level,
        confirmed_domain=confirmed_domain,
        review_priority=priority,
    )


def retry(reason: str, checks: tuple[CheckRecord, ...]) -> VerifyDecision:
    return VerifyDecision(state="retry_wait", reason=reason, checks=checks)
