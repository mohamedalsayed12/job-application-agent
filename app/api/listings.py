"""Stage 13: the logged-in person's matched jobs, best first (T36, T37, D42).

Reading this page never calls the AI. A summary is written when someone opens a job, so a job
nobody has opened yet shows its description here.
"""

from __future__ import annotations

import html
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.cleaning.linking import is_primary
from app.core.db import get_db
from app.models import (
    Applicant,
    ApplicantJobMatch,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    JobSummary,
    SummaryStatus,
)

router = APIRouter(prefix="/listings", tags=["listings"])

PAGE_SIZE = 25
ADZUNA = "adzuna"
_CURRENCY = {"GBP": "£", "USD": "$", "EUR": "€"}
_PERIOD = {"year": "a year", "month": "a month", "week": "a week", "day": "a day", "hour": "an hour"}


@dataclass(frozen=True, slots=True)
class Listing:
    job_id: uuid.UUID
    title: str
    company: str
    places: str
    salary: str
    summary: str | None
    description: str | None
    score: Decimal
    reasons: dict[str, Any]
    matched_skills: list[str]
    missing_skills: list[str]
    missing_licences: list[str]
    last_seen_at: datetime | None
    source: str
    apply_url: str


def salary_text(job: Job) -> str:
    if not job.salary_known or (job.salary_min is None and job.salary_max is None):
        return "Not stated"
    symbol = _CURRENCY.get(job.salary_currency or "", f"{job.salary_currency or ''} ")
    amounts = [f"{symbol}{amount:,.0f}" for amount in (job.salary_min, job.salary_max) if amount is not None]
    span = " to ".join(dict.fromkeys(amounts))
    period = _PERIOD.get(job.salary_period or "", "")
    return f"{span} {period}".strip()


_REGION = {"emea": "EMEA", "apac": "APAC", "americas": "Americas", "europe": "Europe", "worldwide": "Worldwide"}


def place_names(job: Job) -> str:
    names = [
        place.get("city") or place.get("country") or _REGION.get(place.get("region") or "", place.get("region") or "")
        for place in job.places or []
    ]
    text = ", ".join(dict.fromkeys(name for name in names if name)) or "Not stated"
    return f"{text} (remote)" if job.remote_type == "remote" else text


def _sources(session: Session, jobs: list[Job]) -> dict[uuid.UUID, str]:
    by_job: dict[uuid.UUID, list[JobPosting]] = defaultdict(list)
    for job_id, posting in session.execute(
        select(JobLink.job_id, JobPosting)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobLink.job_id.in_([job.id for job in jobs]))
    ).all():
        by_job[job_id].append(posting)
    sources = {}
    for job in jobs:
        postings = by_job.get(job.id, [])
        primary = next((posting for posting in postings if is_primary(job, posting)), postings[0] if postings else None)
        sources[job.id] = primary.source if primary else "unknown"
    return sources


def load_listings(
    session: Session, applicant_id: uuid.UUID, *, everywhere: bool, page_number: int
) -> tuple[list[Listing], int]:
    """One page of open matched jobs, best score first, and how many there are in all."""
    query = (
        select(ApplicantJobMatch, Job)
        .join(Job, Job.id == ApplicantJobMatch.job_id)
        .where(ApplicantJobMatch.applicant_id == applicant_id, Job.freshness == JobFreshness.open)
    )
    if not everywhere:
        query = query.where(ApplicantJobMatch.location_fit.is_(True))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(
        query.order_by(ApplicantJobMatch.score.desc(), Job.company_name, Job.title, Job.id)
        .limit(PAGE_SIZE)
        .offset((page_number - 1) * PAGE_SIZE)
    ).all()
    jobs = [job for _, job in rows]
    summaries = dict(
        session.execute(
            select(JobSummary.job_id, JobSummary.summary_text).where(
                JobSummary.job_id.in_([job.id for job in jobs]), JobSummary.status == SummaryStatus.accepted
            )
        ).all()
    )
    sources = _sources(session, jobs) if jobs else {}
    listings = [
        Listing(
            job_id=job.id,
            title=job.title,
            company=job.company_name,
            places=place_names(job),
            salary=salary_text(job),
            summary=summaries.get(job.id),
            description=job.description,
            score=match.score,
            reasons=match.reasons,
            matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
            missing_licences=match.missing_licences,
            last_seen_at=job.last_seen_at,
            source=sources[job.id],
            apply_url=job.apply_url,
        )
        for match, job in rows
    ]
    return listings, total


def _e(value: object) -> str:
    return html.escape(str(value))


def _apply(url: str) -> str:
    if not url.lower().startswith(("https://", "http://")):
        return "No usable apply link was given."
    return f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">Apply on the original posting</a>'


def _reasons(item: Listing) -> str:
    role = item.reasons.get("role", {})
    level = f", {role['level']}" if role.get("level") else ""
    lines = [
        f"Role: {role.get('kind', 'unknown')} match for {role.get('target', '')}{level}",
        f"Location: {item.reasons.get('location', 'unknown')}",
        f"Salary: {item.reasons.get('salary', 'unknown')}",
        f"Skills you have: {', '.join(item.matched_skills) or 'none named'}",
        f"Skills you don't list: {', '.join(item.missing_skills) or 'none'}",
    ]
    if item.missing_licences:
        lines.append(f"Licences you don't list: {', '.join(item.missing_licences)}")
    return "<ul>" + "".join(f"<li>{_e(line)}</li>" for line in lines) + "</ul>"


def render_listing(item: Listing) -> str:
    if item.summary:
        about = f"<p>{_e(item.summary)}</p><p><small>AI summary, checked against the posting.</small></p>"
    else:
        about = (
            f'<details><summary>Description</summary><div style="white-space: pre-line">'
            f"{_e(item.description or 'No description was given.')}</div></details>"
        )
    seen = item.last_seen_at.strftime("%d %b %Y") if item.last_seen_at else "not recorded"
    label = (
        ' <a href="https://www.adzuna.co.uk/" rel="noopener noreferrer">Jobs by Adzuna</a>'
        if item.source == ADZUNA
        else ""
    )
    return f"""
<article>
<h2><a href="/jobs/{item.job_id}">{_e(item.title)}</a></h2>
<p>{_e(item.company)}. {_e(item.places)}. Salary: {_e(item.salary)}.</p>
<p>Match score {_e(item.score)}.</p>
{_reasons(item)}
{about}
<p>Last confirmed open {_e(seen)}. Source: {_e(item.source)}.{label} {_apply(item.apply_url)}</p>
</article>
"""


def _nav(page_number: int, total: int, everywhere: bool) -> str:
    pages = max(1, -(-total // PAGE_SIZE))
    scope = "&amp;everywhere=1" if everywhere else ""
    links = []
    if page_number > 1:
        links.append(f'<a href="/listings?page={page_number - 1}{scope}">Previous</a>')
    if page_number < pages:
        links.append(f'<a href="/listings?page={page_number + 1}{scope}">Next</a>')
    return f"<p>Page {page_number} of {pages}. {' '.join(links)}</p>"


@router.get("", response_class=HTMLResponse)
def listings_page(
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    page_number: int = Query(1, alias="page", ge=1),
    everywhere: bool = Query(False),
) -> HTMLResponse:
    listings, total = load_listings(db, applicant.id, everywhere=everywhere, page_number=page_number)
    if everywhere:
        scope = '<p>Showing matches in every location. <a href="/listings">Only my locations</a></p>'
    else:
        scope = '<p>Showing matches in your locations. <a href="/listings?everywhere=1">Include other locations</a></p>'
    if not listings:
        body = "<p>No open jobs match your profile yet. Matching runs after each collection.</p>"
    else:
        body = "".join(render_listing(item) for item in listings)
    return page(
        "Your job matches",
        f"<h1>Your job matches</h1><p>{total} open jobs, best match first.</p>{scope}{body}{_nav(page_number, total, everywhere)}",
    )
