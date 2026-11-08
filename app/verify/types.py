"""Domain shapes for Stage 5 verification (MVP §7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.models import EvidenceLevel, VerificationOutcome

VerifyState = Literal["verified", "needs_review", "unsupported", "retry_wait"]

UNSUPPORTED_REVISIT = timedelta(days=90)

# Careers hosts the MVP does not collect from (T17 / Stage 5 step 4).
# Use ATS job hosts only — not the vendor marketing site (e.g. workday.com).
UNSUPPORTED_CAREERS_MARKERS: tuple[tuple[str, str], ...] = (
    ("workday", "myworkdayjobs.com"),
    ("ashby", "ashbyhq.com"),
    ("smartrecruiters", "smartrecruiters.com"),
    ("teamtailor", "teamtailor.com"),
    ("workable", "workable.com"),
    ("successfactors", "successfactors.com"),
    ("successfactors", "successfactors.eu"),
    ("icims", "icims.com"),
    ("taleo", "taleo.net"),
    ("eploy", "eploy.net"),
    ("firefish", "firefishsoftware.com"),
    ("getgot", "getgotjobs.co.uk"),
    ("pinpoint", "pinpointhq.com"),
    ("recruitee", "recruitee.com"),
    ("bamboohr", "bamboohr.com"),
    ("jobvite", "jobvite.com"),
)

# Step 10 R3: a careers page smaller than this is treated as a JavaScript stub,
# not as proof that the company has no public board.
MIN_CAREERS_PAGE_BYTES = 2048


@dataclass(frozen=True, slots=True)
class CheckRecord:
    check_type: str
    outcome: VerificationOutcome
    evidence_url: str | None = None
    evidence_snippet: str | None = None
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class OfficialDomainCheck:
    confirmed: bool
    domain: str | None
    record: CheckRecord
    homepage_html: str | None = None
    homepage_url: str | None = None
    says_agency: bool = False


@dataclass(frozen=True, slots=True)
class OwnershipAssessment:
    level: EvidenceLevel
    record: CheckRecord


@dataclass(frozen=True, slots=True)
class ApiAccessCheck:
    """Public board API outcome after Step 3 clients."""

    access: Literal["confirmed", "not_found", "failed"]
    record: CheckRecord
    instance: str | None = None
    job_count: int | None = None
    company_names: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class UnsupportedCareers:
    system: str
    evidence_url: str
    evidence_snippet: str
    record: CheckRecord


@dataclass(frozen=True, slots=True)
class VerifyDecision:
    state: VerifyState
    reason: str
    checks: tuple[CheckRecord, ...]
    evidence_level: EvidenceLevel | None = None
    careers_system: str | None = None
    revisit_at: datetime | None = None
    board_instance: str | None = None
    last_job_count: int | None = None
    review_priority: int = 50
    confirmed_domain: str | None = None
