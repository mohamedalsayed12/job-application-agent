"""Google Jobs searches through SerpAPI feed the job pipeline (D73)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.google_jobs import collect_google_jobs
from app.indeed import indeed_searches
from app.linkedin import Search
from app.models import ApplicantProfile, BudgetPolicy, ExperienceLevel, FetchOutcome, FetchRun, SpendLedger
from tests.jobs import DAY_1, _google_jobs, _job, _public_resolve
from tests.test_matching import _applicant

_ENTRY = frozenset({ExperienceLevel.entry})
_ENTRY_BACKEND = [
    Search("Graduate Backend Developer", "Manchester", _ENTRY),
    Search("Junior Backend Developer", "Manchester", _ENTRY),
    Search("Entry Level Backend Developer", "Manchester", _ENTRY),
]


def _with_entry_levels(session: Session, applicant_id) -> None:
    session.add(ApplicantProfile(applicant_id=applicant_id, experience_levels=[ExperienceLevel.entry]))
    session.flush()



class _SerpApi:
    """Answers like SerpAPI: one Google Jobs page of ``items`` per search."""

    def __init__(
        self,
        items: list[dict],
        *,
        status: int = 200,
        error: str | None = None,
        body: object | None = None,
    ) -> None:
        self.items, self.status, self.error = items, status, error
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        if self.error is not None:
            return httpx.Response(self.status, json={"error": self.error})
        return httpx.Response(self.status, json={"jobs_results": self.items})

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"serpapi.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )

    def queries(self) -> list[dict[str, list[str]]]:
        return [parse_qs(urlparse(str(request.url)).query) for request in self.requests]


def _policy(session: Session, money_cap: str = "1") -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=Decimal(money_cap),
            currency="GBP",
            effective_from=DAY_1 - timedelta(days=30),
        )
    )
    session.flush()


async def _collect(
    session: Session,
    serp: _SerpApi,
    *,
    at=DAY_1,
    applicant_id: object | None = None,
    **settings: object,
):
    gateway = serp.gateway()
    try:
        opts = {"serpapi_api_key": "test-key", "ai_applicant_ids": str(applicant_id or ""), **settings}
        return await collect_google_jobs(
            session,
            gateway=gateway,
            settings=Settings(**opts),  # type: ignore[arg-type]
            persist=session.flush,
            now=lambda: at,
        )
    finally:
        await gateway.aclose()


async def test_a_search_calls_serpapi_and_stores_what_it_found(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    serp = _SerpApi([_google_jobs(1), _google_jobs(2, title="Python Developer")])

    report = await _collect(session, serp, applicant_id=applicant_id)

    assert (report.searches, report.jobs, report.postings_new, report.failed) == (3, 6, 2, [])
    assert all(request.url.host == "serpapi.com" for request in serp.requests)
    assert all(request.url.path == "/search.json" for request in serp.requests)
    queries = serp.queries()
    assert [q["q"][0] for q in queries] == [f"{search.role} Manchester" for search in _ENTRY_BACKEND]
    # The place is part of the query: SerpAPI's own `location` refuses names like "Manchester, UK".
    assert queries[0] == {
        "engine": ["google_jobs"],
        "q": ["Graduate Backend Developer Manchester"],
        "hl": ["en"],
        "gl": ["uk"],
        "api_key": ["test-key"],
    }
    assert _job(session, 2).title == "Python Developer"
    spent = list(session.scalars(select(SpendLedger)))
    assert len(spent) == 3
    assert {(row.kind, row.provider, row.actual_cost) for row in spent} == {
        ("google_jobs", "serpapi", Decimal("0.0120"))
    }
    runs = list(session.scalars(select(FetchRun)))
    assert len(runs) == 3
    assert {(run.outcome, run.source) for run in runs} == {(FetchOutcome.complete, "google_jobs")}


async def test_searches_run_once_a_day(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    await _collect(session, _SerpApi([_google_jobs(1)]), applicant_id=applicant_id)

    later = _SerpApi([_google_jobs(2)])
    report = await _collect(session, later, at=DAY_1 + timedelta(hours=23), applicant_id=applicant_id)
    assert (report.searches, later.requests) == (0, [])

    report = await _collect(
        session, _SerpApi([_google_jobs(2)]), at=DAY_1 + timedelta(hours=24), applicant_id=applicant_id
    )
    assert report.searches == 3


async def test_without_a_serpapi_key_nothing_is_searched(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    serp = _SerpApi([_google_jobs(1)])
    report = await _collect(session, serp, applicant_id=applicant_id, serpapi_api_key="")
    assert (report.stopped_by, serp.requests) == ("not_configured", [])


async def test_with_no_budget_left_no_search_is_started(session: Session) -> None:
    _policy(session, money_cap="0.001")
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    serp = _SerpApi([_google_jobs(1)])
    report = await _collect(session, serp, applicant_id=applicant_id)
    assert (report.stopped_by, serp.requests) == ("budget", [])


async def test_a_failed_search_is_not_charged_and_does_not_block_the_day(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    report = await _collect(session, _SerpApi([], status=500), applicant_id=applicant_id)

    assert report.failed == [f"{search.role} in Manchester: SerpAPI answered 500" for search in _ENTRY_BACKEND]
    assert {run.outcome for run in session.scalars(select(FetchRun))} == {FetchOutcome.failed}
    # Reservation rows stay for the audit trail, but the actual cost is zero so the cap is free.
    assert {row.actual_cost for row in session.scalars(select(SpendLedger))} == {Decimal("0")}

    retry = _SerpApi([_google_jobs(1)])
    second = await _collect(session, retry, at=DAY_1 + timedelta(hours=1), applicant_id=applicant_id)
    assert second.searches == 3
    assert len(retry.requests) == 3


async def test_an_api_error_message_is_recorded_without_a_fetch_run(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    report = await _collect(session, _SerpApi([], error="Invalid API key."), applicant_id=applicant_id)
    assert report.failed == [f"{search.role} in Manchester: Invalid API key." for search in _ENTRY_BACKEND]
    assert {(run.outcome, run.jobs_returned) for run in session.scalars(select(FetchRun))} == {(FetchOutcome.failed, 0)}


async def test_empty_results_are_a_successful_empty_run(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    report = await _collect(session, _SerpApi([]), applicant_id=applicant_id)
    assert (report.searches, report.jobs, report.postings_new, report.failed) == (3, 0, 0, [])
    assert {(run.outcome, run.jobs_returned) for run in session.scalars(select(FetchRun))} == {
        (FetchOutcome.empty, 0)
    }
    assert session.scalar(select(SpendLedger).limit(1)).actual_cost == Decimal("0.0120")


async def test_google_jobs_reuses_indeed_entry_title_searches(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    assert indeed_searches(session, None) == _ENTRY_BACKEND


async def test_items_without_a_job_id_are_dropped(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _with_entry_levels(session, applicant_id)
    item = _google_jobs("keep-me")
    bare = {"title": "No id", "company_name": "Acme", "location": "Manchester"}
    report = await _collect(session, _SerpApi([item, bare]), applicant_id=applicant_id)
    assert report.postings_new == 1
    assert _job(session, "keep-me").title == "Backend Developer"
