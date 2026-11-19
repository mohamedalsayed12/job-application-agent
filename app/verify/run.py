"""Worker stage: verify open jobs are not fake listings (D55)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.models import Job, JobFreshness, JobVerification, ListingVerdict
from app.verify.companies_house import lookup_company
from app.verify.domain_search import find_official_domain
from app.verify.listing import find_listing_on_site
from app.verify.scam_text import scam_reasons

PROVENANCE_SOURCE = "verification"


@dataclass
class VerifyReport:
    checked: int = 0
    fake: int = 0
    real: int = 0
    inconclusive: int = 0
    upgraded: int = 0
    changed_job_ids: list[uuid.UUID] = field(default_factory=list)


def content_hash(job: Job) -> str:
    raw = "\n".join(
        [
            job.title or "",
            job.company_name or "",
            job.description or "",
            # Hash the LinkedIn/source apply URL when we have already upgraded, so a
            # re-check is not forced every cycle. Provenance marks the upgrade.
            _hash_apply_url(job),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_apply_url(job: Job) -> str:
    provenance = (job.field_provenance or {}).get("apply_url") or {}
    if provenance.get("source") == PROVENANCE_SOURCE:
        return provenance.get("prior_apply_url") or ""
    return job.apply_url or ""


async def verify_jobs(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: datetime | None = None,
) -> VerifyReport:
    checked_at = now or datetime.now(UTC)
    report = VerifyReport()
    backlog = _backlog(session, limit=settings.verify_per_cycle)
    for job in backlog:
        before_url = job.apply_url
        before_outcome = _existing_outcome(session, job.id)
        row = await _verify_one(session, job, gateway=gateway, settings=settings, checked_at=checked_at)
        report.checked += 1
        if row.outcome == ListingVerdict.fake:
            report.fake += 1
        elif row.outcome == ListingVerdict.real:
            report.real += 1
        else:
            report.inconclusive += 1
        if row.listing_found and row.company_apply_url:
            _upgrade_apply_url(job, row.company_apply_url, prior=before_url)
            report.upgraded += 1
        if job.apply_url != before_url or row.outcome != before_outcome:
            report.changed_job_ids.append(job.id)
        persist()
    return report


def _backlog(session: Session, *, limit: int) -> list[Job]:
    open_jobs = list(
        session.scalars(
            select(Job).where(Job.freshness == JobFreshness.open).order_by(Job.last_seen_at.desc())
        ).all()
    )
    existing = {
        row.job_id: row
        for row in session.scalars(select(JobVerification)).all()
    }
    due = []
    for job in open_jobs:
        row = existing.get(job.id)
        if row is None or row.source_hash != content_hash(job):
            due.append(job)
        if len(due) >= limit:
            break
    return due


def _existing_outcome(session: Session, job_id) -> str | None:
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job_id))
    return row.outcome if row else None


async def _verify_one(
    session: Session,
    job: Job,
    *,
    gateway: FetchGateway,
    settings: Settings,
    checked_at: datetime,
) -> JobVerification:
    reasons: list[str] = []
    evidence: dict = {}
    outcome = ListingVerdict.inconclusive
    company_number = None
    company_status = None
    official_domain = None
    listing_found = False
    company_apply_url = None

    text_hits = list(scam_reasons(job.description))
    if text_hits:
        reasons.extend(text_hits)
        evidence["scam_text"] = text_hits
        outcome = ListingVerdict.fake
        return _save(
            session,
            job,
            checked_at=checked_at,
            outcome=outcome,
            reasons=reasons,
            evidence=evidence,
            company_number=None,
            company_status=None,
            official_domain=None,
            listing_found=False,
            company_apply_url=None,
        )

    ch = await lookup_company(gateway, job.company_name, api_key=settings.companies_house_api_key)
    evidence["companies_house"] = {"reason": ch.reason, **ch.evidence}
    if ch.hit:
        company_number = ch.hit.company_number
        company_status = ch.hit.status
        evidence["companies_house"]["title"] = ch.hit.title
    if ch.reason == "company_not_trading":
        reasons.append("company not trading on Companies House")
        outcome = ListingVerdict.fake
        return _save(
            session,
            job,
            checked_at=checked_at,
            outcome=outcome,
            reasons=reasons,
            evidence=evidence,
            company_number=company_number,
            company_status=company_status,
            official_domain=None,
            listing_found=False,
            company_apply_url=None,
        )

    known_title = ch.hit.title if ch.hit and not ch.evidence.get("fuzzy") else None
    domain = await find_official_domain(
        gateway,
        job.company_name,
        apply_url=job.apply_url,
        known_title=known_title,
    )
    evidence["domain"] = {"reason": domain.reason, "candidates": list(domain.candidates)}
    official_domain = domain.domain
    # Only homepage-verified domains may raise confidence / rewrite the apply link.
    if official_domain is None or domain.reason not in {"apply_host_verified", "search_verified"}:
        reasons.append("official domain not confirmed")
        if ch.reason == "company_active":
            reasons.append("company active on Companies House")
        return _save(
            session,
            job,
            checked_at=checked_at,
            outcome=ListingVerdict.inconclusive,
            reasons=reasons or ["inconclusive"],
            evidence=evidence,
            company_number=company_number,
            company_status=company_status,
            official_domain=None,
            listing_found=False,
            company_apply_url=None,
        )

    listing = await find_listing_on_site(gateway, domain=official_domain, title=job.title)
    evidence["listing"] = {
        "reason": listing.reason,
        "pages_checked": list(listing.pages_checked),
    }
    if listing.found and listing.apply_url:
        listing_found = True
        company_apply_url = listing.apply_url
        reasons.append("confirmed on company site")
        if ch.reason == "company_active":
            reasons.append("company active on Companies House")
        outcome = ListingVerdict.real
    else:
        reasons.append("listing not found on company site")
        if ch.reason == "company_active":
            reasons.append("company active on Companies House")
        # Soft: not found on site alone stays inconclusive so check_real keeps medium.
        outcome = ListingVerdict.inconclusive

    return _save(
        session,
        job,
        checked_at=checked_at,
        outcome=outcome,
        reasons=reasons,
        evidence=evidence,
        company_number=company_number,
        company_status=company_status,
        official_domain=official_domain,
        listing_found=listing_found,
        company_apply_url=company_apply_url,
    )


def _save(
    session: Session,
    job: Job,
    *,
    checked_at: datetime,
    outcome: ListingVerdict,
    reasons: list[str],
    evidence: dict,
    company_number: str | None,
    company_status: str | None,
    official_domain: str | None,
    listing_found: bool,
    company_apply_url: str | None,
) -> JobVerification:
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    if row is None:
        row = JobVerification(job_id=job.id, source_hash="", outcome=outcome, checked_at=checked_at)
        session.add(row)
    row.source_hash = content_hash(job)
    row.outcome = outcome
    row.company_number = company_number
    row.company_status = company_status
    row.official_domain = official_domain
    row.listing_found = listing_found
    row.company_apply_url = company_apply_url
    row.reasons = reasons
    row.evidence = evidence
    row.checked_at = checked_at
    session.flush()
    return row


def _upgrade_apply_url(job: Job, company_url: str, *, prior: str) -> None:
    provenance = dict(job.field_provenance or {})
    prior_url = prior
    existing = provenance.get("apply_url") or {}
    if existing.get("source") == PROVENANCE_SOURCE and existing.get("prior_apply_url"):
        prior_url = existing["prior_apply_url"]
    job.apply_url = company_url
    provenance["apply_url"] = {
        "source": PROVENANCE_SOURCE,
        "prior_apply_url": prior_url,
        "company_apply_url": company_url,
    }
    job.field_provenance = provenance
