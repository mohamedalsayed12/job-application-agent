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
    places: list[dict[str, str | None]]
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

    @property
    def place_keys(self) -> frozenset[str]:
        return frozenset((place["city"] or place["country"] or place.get("region") or "").lower() for place in self.places)


@dataclass(frozen=True, slots=True)
class SameAdvert:
    of_job: uuid.UUID
    shares_a_place: bool


@dataclass(frozen=True, slots=True)
class JobQuality:
    not_recent: NotRecent | None
    older_than_60_days: bool
    confidence: Confidence
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    same_advert: SameAdvert | None = None

    @property
    def recent(self) -> bool:
        return self.not_recent is None

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def unique(self) -> bool:
        return self.same_advert is None or not self.same_advert.shares_a_place

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
            "duplicate_of": None if self.unique else str(self.same_advert.of_job),
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
    if not (job.places or job.remote_type):
        missing.append("location")
    if not job.apply_url:
        missing.append("apply_url")
    if len(job.description or "") < MIN_FULL_DESCRIPTION:
        missing.append("full_description")
    return tuple(missing)


def find_same_adverts(jobs: Iterable[JobFacts]) -> dict[uuid.UUID, SameAdvert]:
    """Jobs whose company, title and description match an earlier job within 30 days.

    D34: only those sharing a place with that job are duplicates. The same advert in
    another city is a separate job, with its own apply link.
    """
    adverts: dict[tuple, list[JobFacts]] = defaultdict(list)
    for job in jobs:
        if job.employer_id is not None and job.description:
            adverts[(job.employer_id, title_key(job.title), " ".join(job.description.split()))].append(job)

    found: dict[uuid.UUID, SameAdvert] = {}
    for group in adverts.values():
        group.sort(key=lambda job: (job.listed_since, str(job.job_id)))
        for index, later in enumerate(group):
            nearby = [
                earlier
                for earlier in group[:index]
                if later.listed_since - earlier.listed_since <= REPOST_WINDOW
            ]
            if nearby:
                sharing = [earlier for earlier in nearby if _shares_a_place(earlier, later)]
                found[later.job_id] = SameAdvert((sharing or nearby)[-1].job_id, bool(sharing))
    return found


def _shares_a_place(first: JobFacts, second: JobFacts) -> bool:
    """Two jobs naming no place at all cannot be told apart, so they count as the same place."""
    return first.place_keys == second.place_keys or bool(first.place_keys & second.place_keys)


def check_job(job: JobFacts, now: datetime, same_advert: SameAdvert | None = None) -> JobQuality:
    confidence, reasons = check_real(job)
    return JobQuality(
        not_recent=check_recent(job, now),
        older_than_60_days=now - job.listed_since > FLAG_OLDER_THAN,
        confidence=confidence,
        reasons=reasons,
        missing=check_complete(job),
        same_advert=same_advert,
    )
