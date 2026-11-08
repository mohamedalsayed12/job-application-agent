"""Link postings to master jobs: exact job IDs first, then strong matches. Uncertain pairs stay separate."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Employer, Job, JobFreshness, JobLink, JobLinkMethod, JobPosting

BOARD_SOURCES = frozenset({"greenhouse", "lever"})
NEARBY_DATES = timedelta(days=14)
FIRST_POSTING_REASON = "first posting of this job"

_GREENHOUSE_PATH = re.compile(r"^/[^/]+/jobs/(\d+)")
_LEVER_PATH = re.compile(r"^/[^/]+/([0-9a-fA-F-]{36})(?:/|$)")
_LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
_MIRRORED = (
    "title",
    "company_name",
    "location_city",
    "location_country",
    "remote_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_known",
    "description",
    "apply_url",
)


def board_job_key(url: str | None) -> tuple[str, str] | None:
    """The board system and job ID named by an apply link, if it names one."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in _LEVER_HOSTS:
        found = _LEVER_PATH.match(parts.path)
        return ("lever", found.group(1).lower()) if found else None
    query = parse_qs(parts.query)
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        found = _GREENHOUSE_PATH.match(parts.path)
        if found:
            return "greenhouse", found.group(1)
        if parts.path.startswith("/embed/job_app"):
            tokens = query.get("token", [])
            if tokens and tokens[0].isdigit():
                return "greenhouse", tokens[0]
    job_ids = query.get("gh_jid", [])
    return ("greenhouse", job_ids[0]) if job_ids and job_ids[0].isdigit() else None


def title_key(title: str) -> str:
    return " ".join(title.lower().split())


def link_posting(session: Session, posting: JobPosting) -> JobLink:
    """Safe to repeat: a posting that already has a link keeps it."""
    existing = session.scalar(select(JobLink).where(JobLink.job_posting_id == posting.id))
    if existing is not None:
        return existing

    exact = _exact_match(session, posting)
    if exact is not None:
        job_id, reason = exact
        return _link(session, posting, job_id, JobLinkMethod.exact_job_id, Decimal("1"), reason)
    strong = _strong_match(session, posting)
    if strong is not None:
        reason = f"same employer, title and city, posted within {NEARBY_DATES.days} days"
        return _link(session, posting, strong, JobLinkMethod.strong_match, Decimal("0.9"), reason)

    job = Job(
        employer_id=posting.employer_id,
        sector=_sector(session, posting),
        freshness=JobFreshness.open,
        field_provenance={},
        first_seen_at=posting.first_seen_at,
        last_seen_at=posting.last_seen_at,
        title=posting.title,
        company_name=posting.company_name,
        salary_known=posting.salary_known,
        apply_url=posting.apply_url,
    )
    session.add(job)
    mirror_posting(job, posting)
    session.flush()
    return _link(session, posting, job.id, JobLinkMethod.exact_job_id, Decimal("1"), FIRST_POSTING_REASON)


def mirror_posting(job: Job, posting: JobPosting) -> None:
    """Copy a posting's fields onto its job and record where each value came from."""
    provenance = {
        "source": posting.source,
        "posting_id": str(posting.id),
        "raw_payload_id": str(posting.raw_payload_id),
    }
    for name in _MIRRORED:
        setattr(job, name, getattr(posting, name))
    job.field_provenance = {
        name: provenance for name in _MIRRORED if getattr(posting, name) is not None
    }


def is_primary(job: Job, posting: JobPosting) -> bool:
    return (job.field_provenance.get("title") or {}).get("posting_id") == str(posting.id)


def _link(
    session: Session,
    posting: JobPosting,
    job_id: uuid.UUID,
    method: JobLinkMethod,
    confidence: Decimal,
    reason: str,
) -> JobLink:
    link = JobLink(job_posting_id=posting.id, job_id=job_id, method=method, confidence=confidence, reason=reason)
    session.add(link)
    session.flush()
    return link


def _exact_match(session: Session, posting: JobPosting) -> tuple[uuid.UUID, str] | None:
    if posting.source in BOARD_SOURCES:
        return None
    key = board_job_key(posting.apply_url)
    if key is None:
        return None
    job_id = session.scalar(
        select(JobLink.job_id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobPosting.source == key[0], JobPosting.source_job_id == key[1])
    )
    return (job_id, f"apply link names {key[0]} job {key[1]}") if job_id else None


def _strong_match(session: Session, posting: JobPosting) -> uuid.UUID | None:
    """Without an employer, a city and a posted date on both sides the pair is uncertain.

    Two listings from one source may be two real openings, so they also need the same advert text.
    """
    if posting.employer_id is None or posting.location_city is None or posting.posted_at is None:
        return None
    rows = session.execute(
        select(JobPosting.title, JobPosting.posted_at, JobPosting.source, JobPosting.description, Job.id)
        .join(JobLink, JobLink.job_posting_id == JobPosting.id)
        .join(Job, Job.id == JobLink.job_id)
        .where(
            JobPosting.employer_id == posting.employer_id,
            JobPosting.id != posting.id,
            JobPosting.posted_at.is_not(None),
            func.lower(JobPosting.location_city) == posting.location_city.lower(),
            Job.freshness != JobFreshness.closed,
        )
    ).all()
    key = title_key(posting.title)
    jobs = {
        job_id
        for title, posted_at, source, description, job_id in rows
        if title_key(title) == key
        and abs(posted_at - posting.posted_at) <= NEARBY_DATES
        and (source != posting.source or (description is not None and description == posting.description))
    }
    return jobs.pop() if len(jobs) == 1 else None


def _sector(session: Session, posting: JobPosting) -> str:
    employer = session.get(Employer, posting.employer_id) if posting.employer_id else None
    return employer.sector if employer else "unknown"
