"""Link postings to master jobs by strong match. Uncertain pairs stay separate."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applicants.service import MILESTONE1_SECTOR
from app.models import Job, JobFreshness, JobLink, JobLinkMethod, JobPosting

NEARBY_DATES = timedelta(days=14)
FIRST_POSTING_REASON = "first posting of this job"

_MIRRORED = (
    "title",
    "company_name",
    "location_city",
    "location_country",
    "remote_type",
    "places",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_known",
    "description",
    "apply_url",
)


def title_key(title: str) -> str:
    return " ".join(title.lower().split())


def link_posting(session: Session, posting: JobPosting) -> JobLink:
    """Safe to repeat: a posting that already has a link keeps it."""
    existing = session.scalar(select(JobLink).where(JobLink.job_posting_id == posting.id))
    if existing is not None:
        return existing

    strong = _strong_match(session, posting)
    if strong is not None:
        reason = f"same company, title and city, posted within {NEARBY_DATES.days} days"
        return _link(session, posting, strong, JobLinkMethod.strong_match, Decimal("0.9"), reason)

    job = Job(
        employer_id=posting.employer_id,
        sector=MILESTONE1_SECTOR,
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


def _strong_match(session: Session, posting: JobPosting) -> uuid.UUID | None:
    """Without a city and a posted date on both sides the pair is uncertain.

    Two listings from one source may be two real openings, so they also need the same advert text.
    """
    if posting.location_city is None or posting.posted_at is None:
        return None
    rows = session.execute(
        select(JobPosting.title, JobPosting.posted_at, JobPosting.source, JobPosting.description, Job.id)
        .join(JobLink, JobLink.job_posting_id == JobPosting.id)
        .join(Job, Job.id == JobLink.job_id)
        .where(
            func.lower(JobPosting.company_name) == posting.company_name.lower(),
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
