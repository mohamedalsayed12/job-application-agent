"""Shared test helpers: LinkedIn results stored through the real storage and cleaning path."""

from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway
from app.linkedin import store_items
from app.models import ApplicantJobMatch, Job, JobLink, JobPosting, SalaryStatus

DAY_1 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _li(
    job_id: int | str,
    *,
    title: str = "Backend Developer",
    city: str = "Manchester, UK",
    content: str = "Build APIs in Python.",
    company: str = "Acme",
    posted: str = "2026-11-01T00:00:00.000Z",
    **extra: Any,
) -> dict[str, Any]:
    """One result as the Apify LinkedIn scraper returns it."""
    return {
        "linkedinJobId": str(job_id),
        "title": title,
        "company": company,
        "location": city,
        "postedAt": posted,
        "applyUrl": f"https://acme.example/careers/{job_id}",
        "jobUrl": f"https://www.linkedin.com/jobs/view/{job_id}",
        "description": content,
        "employmentType": "Full-time",
        **extra,
    }


def _indeed(
    job_id: int | str,
    *,
    title: str = "Backend Developer",
    city: str = "Manchester",
    country: str = "GB",
    content: str = "Build APIs in Python.",
    company: str = "Acme",
    posted: str = "2026-11-01T00:00:00.000Z",
    apply_url: str | None = None,
    salary: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One result as Apify valig/indeed-jobs-scraper returns it."""
    key = str(job_id)
    return {
        "key": key,
        "url": f"https://uk.indeed.com/viewjob?jk={key}",
        "title": title,
        "jobUrl": apply_url if apply_url is not None else f"https://acme.example/careers/{key}",
        "datePublished": posted,
        "location": {"city": city, "countryCode": country, "countryName": "United Kingdom"},
        "employer": {"name": company},
        "description": {"text": content, "html": f"<p>{content}</p>"},
        "jobTypes": {"CF3CP": "Full-time"},
        "baseSalary": salary,
        **extra,
    }


def _google_jobs(
    job_id: int | str,
    *,
    title: str = "Backend Developer",
    city: str = "Manchester, England",
    content: str = "Build APIs in Python.",
    company: str = "Acme",
    apply_url: str | None = None,
    schedule: str = "Full-time",
    **extra: Any,
) -> dict[str, Any]:
    """One result as SerpAPI's Google Jobs engine returns it."""
    key = str(job_id)
    link = apply_url if apply_url is not None else f"https://acme.example/careers/{key}"
    return {
        "job_id": key,
        "title": title,
        "company_name": company,
        "location": city,
        "description": content,
        "share_link": f"https://www.google.com/search?q=job+{key}",
        "apply_options": [{"title": "Apply", "link": link}],
        "detected_extensions": {"schedule_type": schedule},
        **extra,
    }


class _Apify:
    """Answers like Apify: a run starts, is still running at the first check, then succeeds with ``items``."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        status: str = "SUCCEEDED",
        cost_usd: float = 0.02,
        start: bool = True,
    ) -> None:
        self.items, self.status, self.cost_usd, self.start = items, status, cost_usd, start
        self.requests: list[httpx.Request] = []
        self.checks = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if not self.start:
                return httpx.Response(201, json={"data": {}})
            run = {"id": f"run{len(self.requests)}", "defaultDatasetId": "data1", "usageTotalUsd": self.cost_usd}
            return httpx.Response(201, json={"data": {**run, "status": "RUNNING"}})
        run = {"id": f"run{len(self.requests)}", "defaultDatasetId": "data1", "usageTotalUsd": self.cost_usd}
        if path.startswith("/v2/actor-runs/"):
            self.checks += 1
            return httpx.Response(200, json={"data": {**run, "status": "RUNNING" if self.checks == 1 else self.status}})
        if path == "/v2/datasets/data1/items":
            return httpx.Response(200, json=self.items)
        return httpx.Response(404, json={"error": "unexpected"})

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.apify.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests if request.method == "POST" and request.content]


def _store(session: Session, items: list[dict[str, Any]], *, at: datetime = DAY_1) -> int:
    return store_items(session, items, started_at=at, finished_at=at)


def _job(session: Session, source_job_id: int | str) -> Job:
    return session.scalar(
        select(Job)
        .join(JobLink, JobLink.job_id == Job.id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobPosting.source_job_id == str(source_job_id))
    )


def show_to(session: Session, applicant_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Puts the job in this person's listings, so its page and actions open for them."""
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant_id,
            job_id=job_id,
            score=Decimal("1.0"),
            reasons={},
            role_match=True,
            location_fit=True,
            salary_status=SalaryStatus.unknown,
        )
    )
    session.flush()
