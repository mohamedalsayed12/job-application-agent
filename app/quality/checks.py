"""The per-job quality measures from MVP section 14: Recent, Real, Complete and Unique."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from app.cleaning.linking import title_key
from app.models import BoardStatus, JobFreshness

LISTED_WITHIN = timedelta(hours=48)
FLAG_OLDER_THAN = timedelta(days=60)
REPOST_WINDOW = timedelta(days=30)
# Search snippets run to about 300 characters. The shortest real advert stored so far has 605.
MIN_FULL_DESCRIPTION = 300

# Payment and fintech adverts talk about fees and payments all the time, so these need the request itself.
_SCAM_SIGNS = (
    (
        "asks the applicant for money",
        re.compile(
            r"\b(?:registration|training|processing|admin(?:istration)?) fees?\b"
            r"|\bpay (?:a|an|the|your) (?:\w+ )?(?:fee|deposit)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "chat-only contact",
        re.compile(r"\b(?:contact|message|text|reach|apply)\b[^.]{0,40}\b(?:whatsapp|telegram)\b", re.IGNORECASE),
    ),
)
_NOT_A_ROLE = re.compile(
    r"talent (?:community|pool|network|pipeline)|can.t find|general application|speculative|expression of interest",
    re.IGNORECASE,
)
_BOARD_HOST_SUFFIXES = (".greenhouse.io", ".lever.co")


class Confidence(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


_RANK = {Confidence.low: 0, Confidence.medium: 1, Confidence.high: 2}


class NotRecent(StrEnum):
    closed = "closed"
    past_closing_date = "past_closing_date"
    not_seen_in_48_hours = "not_seen_in_48_hours"


@dataclass(frozen=True, slots=True)
class JobFacts:
    job_id: uuid.UUID
    employer_id: uuid.UUID | None
    company_name: str
    title: str
    description: str | None
    apply_url: str | None
    location_city: str | None
    location_country: str | None
    remote_type: str | None
    freshness: str
    last_seen_at: datetime
    first_seen_at: datetime
    posted_at: datetime | None
    closes_at: datetime | None
    board_status: str | None
    official_domain: str | None
    source: str
    sector: str

    @property
    def listed_since(self) -> datetime:
        return self.posted_at or self.first_seen_at


@dataclass(frozen=True, slots=True)
class Duplicate:
    of_job: uuid.UUID
    same_city: bool


@dataclass(frozen=True, slots=True)
class JobQuality:
    not_recent: NotRecent | None
    older_than_60_days: bool
    confidence: Confidence
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    duplicate: Duplicate | None = None

    @property
    def recent(self) -> bool:
        return self.not_recent is None

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def unique(self) -> bool:
        return self.duplicate is None

    def to_json(self, checked_at: datetime) -> dict:
        return {
            "checked_at": checked_at.isoformat(),
            "recent": self.recent,
            "not_recent": self.not_recent,
            "older_than_60_days": self.older_than_60_days,
            "real": self.confidence,
            "real_reasons": list(self.reasons),
            "complete": self.complete,
            "missing": list(self.missing),
            "unique": self.unique,
            "duplicate_of": str(self.duplicate.of_job) if self.duplicate else None,
        }


def check_recent(job: JobFacts, now: datetime) -> NotRecent | None:
    if job.freshness == JobFreshness.closed:
        return NotRecent.closed
    if job.closes_at is not None and job.closes_at <= now:
        return NotRecent.past_closing_date
    if now - job.last_seen_at > LISTED_WITHIN:
        return NotRecent.not_seen_in_48_hours
    return None


def check_real(job: JobFacts) -> tuple[Confidence, tuple[str, ...]]:
    """The confidence is the lowest any signal allows. Companies House is not connected yet."""
    signals = [_board_signal(job), _apply_link_signal(job)]
    if _NOT_A_ROLE.search(job.title):
        signals.append((Confidence.medium, "a general sign-up, not a specific role"))
    signals.extend(
        (Confidence.low, reason) for reason, pattern in _SCAM_SIGNS if pattern.search(job.description or "")
    )
    return (
        min((level for level, _ in signals), key=_RANK.__getitem__),
        tuple(reason for _, reason in signals),
    )


def _board_signal(job: JobFacts) -> tuple[Confidence, str]:
    if job.board_status == BoardStatus.verified:
        return Confidence.high, "verified official board"
    if job.board_status == BoardStatus.needs_reverify:
        return Confidence.medium, "official board needs checking again"
    return Confidence.low, "not from a verified official board"


def _apply_link_signal(job: JobFacts) -> tuple[Confidence, str]:
    host = (urlsplit(job.apply_url or "").hostname or "").lower()
    domain = (job.official_domain or "").lower()
    if domain and (host == domain or host.endswith(f".{domain}")):
        return Confidence.high, "apply link on the company's domain"
    if job.board_status == BoardStatus.verified and host.endswith(_BOARD_HOST_SUFFIXES):
        return Confidence.high, "apply link on the company's board"
    return Confidence.medium, "apply link is not on the company's domain or board"


def check_complete(job: JobFacts) -> tuple[str, ...]:
    """Employer name and salary are tracked but optional, so they are not asked for here."""
    missing = []
    if not job.title.strip():
        missing.append("title")
    if not (job.location_city or job.location_country or job.remote_type):
        missing.append("location")
    if not job.apply_url:
        missing.append("apply_url")
    if len(job.description or "") < MIN_FULL_DESCRIPTION:
        missing.append("full_description")
    return tuple(missing)


def find_duplicates(jobs: Iterable[JobFacts]) -> dict[uuid.UUID, Duplicate]:
    """D17: a job is a repost when company, title and description match another within 30 days.

    Comparing each job with the one listed just before it means a chain of reposts counts
    as one job, and only the extras count as duplicates.
    """
    adverts: dict[tuple, list[JobFacts]] = defaultdict(list)
    for job in jobs:
        if job.employer_id is not None and job.description:
            adverts[(job.employer_id, title_key(job.title), " ".join(job.description.split()))].append(job)

    duplicates: dict[uuid.UUID, Duplicate] = {}
    for group in adverts.values():
        group.sort(key=lambda job: (job.listed_since, str(job.job_id)))
        for earlier, later in zip(group, group[1:], strict=False):
            if later.listed_since - earlier.listed_since <= REPOST_WINDOW:
                same_city = (earlier.location_city or "").lower() == (later.location_city or "").lower()
                duplicates[later.job_id] = Duplicate(of_job=earlier.job_id, same_city=same_city)
    return duplicates


def check_job(job: JobFacts, now: datetime, duplicate: Duplicate | None = None) -> JobQuality:
    confidence, reasons = check_real(job)
    return JobQuality(
        not_recent=check_recent(job, now),
        older_than_60_days=now - job.listed_since > FLAG_OLDER_THAN,
        confidence=confidence,
        reasons=reasons,
        missing=check_complete(job),
        duplicate=duplicate,
    )
