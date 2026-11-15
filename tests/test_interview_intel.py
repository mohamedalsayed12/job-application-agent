"""Interview intel: collect, ground, cache, budget, and profile page."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.interview.check import ground_pack
from app.interview.collect import collect_for_role, store_items
from app.interview.normalize import normalize_item, queries_for
from app.interview.plan import pack_from_payload_for_tests
from app.interview.roles import role_key
from app.interview.service import enqueue, process_requests
from app.main import app
from app.models import (
    BudgetPolicy,
    FetchRun,
    InterviewEvidence,
    InterviewIntelPack,
    InterviewOutcome,
    InterviewPlatform,
    InterviewRequestStatus,
    RawPayload,
    SpendKind,
    SpendLedger,
)
from tests.jobs import DAY_1, _Apify
from tests.test_matching import _applicant

PASSWORD = "correct horse battery"
PROFILE = {
    "target_roles": "Backend Developer",
    "locations": "Manchester",
    "experience_level": "entry",
    "travel_distance": "",
    "salary_amount": "",
    "salary_currency": "GBP",
    "salary_period": "year",
    "skills": "Python",
    "degrees": "",
    "cert_name": [""],
    "cert_issuer": [""],
    "cert_kind": ["certification"],
    "cert_expiry": [""],
}


def _policy(session: Session, money_cap: str = "10") -> None:
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


def _settings(**extra: object) -> Settings:
    return Settings(
        **{
            "apify_api_token": "test-token",
            "anthropic_api_key": "test-key",
            "interview_max_per_platform": 5,
            "interview_wait_minutes": 1,
            **extra,
        }
    )  # type: ignore[arg-type]


def _reddit_item(n: int, *, text: str, title: str = "Got the offer") -> dict[str, Any]:
    return {
        "id": f"r{n}",
        "url": f"https://www.reddit.com/r/cscareerquestions/comments/{n}/post/",
        "title": title,
        "body": text,
    }


def _youtube_item(n: int, *, text: str) -> dict[str, Any]:
    return {
        "id": f"yt{n}",
        "url": f"https://www.youtube.com/watch?v=vid{n}",
        "title": f"Interview tips {n}",
        "description": text,
    }


def _linkedin_item(n: int, *, text: str) -> dict[str, Any]:
    return {
        "id": f"li{n}",
        "url": f"https://www.linkedin.com/posts/user-{n}",
        "title": "LinkedIn post",
        "text": text,
    }


class _MultiApify(_Apify):
    """One Apify mock that returns different items per actor path."""

    def __init__(self, by_actor: dict[str, list[dict[str, Any]]], *, cost_usd: float = 0.02) -> None:
        super().__init__([], cost_usd=cost_usd)
        self.by_actor = by_actor
        self._dataset_items: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        run = {"id": f"run{len(self.requests)}", "defaultDatasetId": f"data{len(self.requests)}", "usageTotalUsd": self.cost_usd}
        if request.method == "POST" and "/acts/" in path and path.endswith("/runs"):
            actor = path.split("/acts/")[1].split("/runs")[0].replace("~", "/")
            self._dataset_items = self.by_actor.get(actor, [])
            self._pending_dataset = run["defaultDatasetId"]
            self._pending_items = list(self._dataset_items)
            return httpx.Response(201, json={"data": {**run, "status": "RUNNING"}})
        if path.startswith("/v2/actor-runs/") and path.endswith("/abort"):
            return httpx.Response(200, json={"data": run})
        if path.startswith("/v2/actor-runs/"):
            self.checks += 1
            return httpx.Response(
                200, json={"data": {**run, "defaultDatasetId": getattr(self, "_pending_dataset", "data1"), "status": "RUNNING" if self.checks % 2 else "SUCCEEDED"}}
            )
        if path.startswith("/v2/datasets/") and path.endswith("/items"):
            return httpx.Response(200, json=getattr(self, "_pending_items", []))
        return httpx.Response(404, json={"error": "unexpected"})


def test_role_key_collapses_case_and_space() -> None:
    assert role_key("  Junior   Software Engineer ") == "junior software engineer"


def test_queries_name_the_role() -> None:
    assert "Backend Developer interview got the offer" in queries_for("Backend Developer")


def test_normalize_rejects_non_http() -> None:
    assert normalize_item(InterviewPlatform.reddit, {"url": "javascript:alert(1)", "body": "x"}, max_snippet=100) is None


def test_ground_pack_keeps_youtube_video_id() -> None:
    allowed = {"https://www.youtube.com/watch?v=vid1"}
    ok = ground_pack(
        {
            "summary": "Watch this.",
            "success_themes": [],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Video", "url": "https://www.youtube.com/watch?v=vid1", "how_used": "watched"}],
            "practice_plan": [],
        },
        allowed,
    )
    bad = ground_pack(
        {
            "summary": "Watch this.",
            "success_themes": [],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Video", "url": "https://www.youtube.com/watch?v=other", "how_used": "watched"}],
            "practice_plan": [],
        },
        allowed,
    )
    assert ok == []
    assert any("other" in problem for problem in bad)


def test_ground_pack_rejects_fabricated_url() -> None:
    problems = ground_pack(
        {
            "summary": "Practice system design.",
            "success_themes": [{"theme": "prep", "detail": "mocked", "cites": ["https://www.reddit.com/r/x/1"]}],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Fake book", "url": "https://evil.example/book", "how_used": "read it"}],
            "practice_plan": [],
        },
        {"https://www.reddit.com/r/x/1"},
    )
    assert any("evil.example" in problem for problem in problems)


def test_ground_pack_accepts_evidence_urls() -> None:
    url = "https://www.reddit.com/r/cscareerquestions/comments/1/post/"
    problems = ground_pack(
        {
            "summary": "People who practised STAR stories did better.",
            "success_themes": [{"theme": "STAR", "detail": "structured answers", "cites": [url]}],
            "failure_themes": [],
            "differentiators": [{"theme": "practice", "detail": "mock interviews", "cites": [url]}],
            "materials": [{"name": "This thread", "url": url, "how_used": "read outcomes"}],
            "practice_plan": [{"order": 1, "method": "STAR drills", "detail": "daily", "resource_urls": [url]}],
        },
        {url},
    )
    assert problems == []


def test_store_items_creates_fetch_run_payload_and_evidence(session: Session) -> None:
    created = store_items(
        session,
        InterviewPlatform.reddit,
        [_reddit_item(1, text="I got the offer after grinding LeetCode.")],
        role_key="backend developer",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    assert created == 1
    evidence = session.scalars(select(InterviewEvidence)).one()
    assert evidence.platform == "reddit"
    assert evidence.url.startswith("https://www.reddit.com/")
    assert session.scalars(select(FetchRun)).one().source == "interview_reddit"
    assert session.scalars(select(RawPayload)).one().source_job_id == "r1"


async def test_collect_reserves_budget_and_stores_three_platforms(session: Session) -> None:
    _policy(session)
    apify = _MultiApify(
        {
            "trudax/reddit-scraper-lite": [_reddit_item(1, text="Got the offer after mock interviews.")],
            "streamers/youtube-scraper": [_youtube_item(1, text="I failed because I froze on system design.")],
            "apimaestro/linkedin-posts-search-scraper-no-cookies": [
                _linkedin_item(1, text="Rejected after skipping behavioural prep.")
            ],
        }
    )
    gateway = apify.gateway()
    try:
        report = await collect_for_role(
            session,
            role="Backend Developer",
            role_key="backend developer",
            gateway=gateway,
            settings=_settings(),
            persist=session.flush,
            now=lambda: DAY_1,
            sleep=lambda _: None,
        )
    finally:
        await gateway.aclose()

    assert report.platforms == 3
    assert report.evidence_new == 3
    assert report.stopped_by is None
    kinds = [row.kind for row in session.scalars(select(SpendLedger))]
    assert kinds == [SpendKind.interview_collect.value] * 3
    actors = [req.url.path for req in apify.requests if req.method == "POST" and req.url.path.endswith("/runs")]
    assert any("trudax~reddit-scraper-lite" in path for path in actors)
    assert any("streamers~youtube-scraper" in path for path in actors)


async def test_collect_stays_queued_when_budget_is_zero(session: Session) -> None:
    _policy(session, money_cap="0")
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    request = enqueue(session, applicant_id=applicant, role="Backend Developer", now=DAY_1)
    assert request.status == InterviewRequestStatus.queued.value

    apify = _MultiApify({"trudax/reddit-scraper-lite": [_reddit_item(1, text="x")]})
    gateway = apify.gateway()
    try:
        report = await process_requests(
            session,
            gateway=gateway,
            settings=_settings(),
            persist=session.flush,
            now=lambda: DAY_1,
            sleep=lambda _: None,
        )
    finally:
        await gateway.aclose()

    session.refresh(request)
    assert report.requeued == 1
    assert report.stopped_by == "budget"
    assert request.status == InterviewRequestStatus.queued.value
    assert session.scalars(select(InterviewEvidence)).all() == []


async def test_cache_hit_skips_second_collect(session: Session) -> None:
    _policy(session)
    url = "https://www.reddit.com/r/cscareerquestions/comments/1/post/"
    store_items(
        session,
        InterviewPlatform.reddit,
        [_reddit_item(1, text="I got the offer using STAR stories.")],
        role_key="backend developer",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    evidence = session.scalars(select(InterviewEvidence)).one()
    evidence.outcome = InterviewOutcome.success.value
    pack = pack_from_payload_for_tests(
        session,
        role="Backend Developer",
        role_key="backend developer",
        payload={
            "summary": "STAR practice helped.",
            "success_themes": [{"theme": "STAR", "detail": "structured answers", "cites": [url]}],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Thread", "url": url, "how_used": "read it"}],
            "practice_plan": [{"order": 1, "method": "STAR", "detail": "daily", "resource_urls": [url]}],
        },
        rows=[evidence],
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(pack, InterviewIntelPack)

    first = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    second = _applicant(session, roles=["Backend Developer"], locations=["Leeds"])
    a = enqueue(session, applicant_id=first, role="Backend Developer", now=DAY_1)
    b = enqueue(session, applicant_id=second, role="Backend Developer", now=DAY_1)
    assert a.status == b.status == InterviewRequestStatus.ready.value
    assert a.pack_id == b.pack_id == pack.id

    apify = _MultiApify({"trudax/reddit-scraper-lite": [_reddit_item(99, text="should not run")]})
    gateway = apify.gateway()
    try:
        report = await process_requests(
            session,
            gateway=gateway,
            settings=_settings(),
            persist=session.flush,
            now=lambda: DAY_1,
            sleep=lambda _: None,
        )
    finally:
        await gateway.aclose()
    assert report.processed == 0
    assert apify.requests == []


def test_pack_from_payload_rejects_ungrounded(session: Session) -> None:
    store_items(
        session,
        InterviewPlatform.youtube,
        [_youtube_item(1, text="Failed after skipping mocks.")],
        role_key="backend developer",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    evidence = session.scalars(select(InterviewEvidence)).one()
    problems = pack_from_payload_for_tests(
        session,
        role="Backend Developer",
        role_key="backend developer",
        payload={
            "summary": "x",
            "success_themes": [],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Nope", "url": "https://fabricated.example/x", "how_used": "n/a"}],
            "practice_plan": [],
        },
        rows=[evidence],
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(problems, list)
    assert problems


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def test_profile_card_and_ready_pack_page(client: httpx.AsyncClient, session: Session) -> None:
    assert (await client.post("/register", data={"email": "intel@example.com", "password": PASSWORD})).status_code == 303
    assert (await client.post("/profile", data=PROFILE)).status_code == 303
    profile = await client.get("/profile")
    assert "Interview intel" in profile.text
    assert 'action="/profile/interview-intel"' in profile.text

    url = "https://www.reddit.com/r/cscareerquestions/comments/1/post/"
    store_items(
        session,
        InterviewPlatform.reddit,
        [_reddit_item(1, text="I got the offer using STAR stories.")],
        role_key="backend developer",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    evidence = session.scalars(select(InterviewEvidence)).one()
    evidence.outcome = InterviewOutcome.success.value
    pack = pack_from_payload_for_tests(
        session,
        role="Backend Developer",
        role_key="backend developer",
        payload={
            "summary": "STAR practice helped people pass.",
            "success_themes": [{"theme": "STAR", "detail": "structured answers", "cites": [url]}],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Thread", "url": url, "how_used": "read it"}],
            "practice_plan": [{"order": 1, "method": "STAR drills", "detail": "daily", "resource_urls": [url]}],
        },
        rows=[evidence],
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(pack, InterviewIntelPack)
    session.commit()

    create = await client.post("/profile/interview-intel", data={"role": "Backend Developer"})
    assert create.status_code == 303
    assert create.headers["location"].startswith("/profile/interview-intel?")
    page = await client.get(create.headers["location"])
    assert page.status_code == 200
    assert "Findings summary" in page.text
    assert "STAR practice helped people pass." in page.text
    assert "Practice plan" in page.text
    assert "STAR drills" in page.text
