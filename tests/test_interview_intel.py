"""Interview intel: collect, ground, cache, budget, and profile page."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.ai.anthropic import INTERVIEW_TIMEOUT_S, send_anthropic_request
from app.api.forms import require_csrf
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchRefusal
from app.interview.check import ground_pack
from app.interview.collect import collect_for_role, store_items
from app.interview.normalize import normalize_item, queries_for
from app.interview.plan import PROMPT_VERSION, fresh_pack, pack_from_payload_for_tests
from app.interview.roles import role_key
from app.interview.stance import read_stance
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
    "experience_levels": ["entry"],
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


async def test_shared_ai_timeout_stays_at_ten_seconds() -> None:
    seen: list[float] = []

    class _Gateway:
        async def fetch(self, request):
            seen.append(request.timeout_s)
            return FetchRefusal(reason="timeout")

    gateway = _Gateway()
    await send_anthropic_request(gateway, {"model": "m"}, api_key="k", fallback_model="m")
    await send_anthropic_request(
        gateway, {"model": "m"}, api_key="k", fallback_model="m", timeout_s=INTERVIEW_TIMEOUT_S
    )
    assert seen == [10.0, INTERVIEW_TIMEOUT_S]


def test_role_key_collapses_case_and_space() -> None:
    assert role_key("  Junior   Software Engineer ") == "junior software engineer"


def test_queries_name_the_role() -> None:
    queries = queries_for("Backend Developer")
    assert queries[0] == 'Backend Developer interview "got the offer"'
    assert all("preparation tips" not in query for query in queries)


def test_normalize_reads_reddit_and_linkedin_post_urls() -> None:
    reddit = normalize_item(
        InterviewPlatform.reddit,
        {"postUrl": "https://www.reddit.com/r/cscareerquestionsuk/comments/abc/offer/", "title": "Offer", "body": "I got the offer."},
        max_snippet=200,
    )
    linkedin = normalize_item(
        InterviewPlatform.linkedin,
        {"post_url": "https://www.linkedin.com/posts/person-1", "text": "I got the offer for a graduate role."},
        max_snippet=200,
    )
    assert reddit is not None and reddit.url.endswith("/offer/")
    assert linkedin is not None and linkedin.url == "https://www.linkedin.com/posts/person-1"


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


def test_success_themes_count_only_someone_who_passed_this_role(session: Session) -> None:
    passer = "https://www.reddit.com/r/cscareerquestionsuk/comments/da/offer/"
    scientist = "https://www.reddit.com/r/datascience/comments/ds/offer/"
    engineer = "https://www.reddit.com/r/cscareerquestions/comments/swe/offer/"
    coach = "https://www.youtube.com/watch?v=coach1"
    tips = "https://www.youtube.com/watch?v=tips1"
    store_items(
        session,
        InterviewPlatform.reddit,
        [
            {"id": "da", "url": passer, "title": "Got the offer", "body": "I got the offer for a data analyst role after the take-home."},
            {"id": "ds", "url": scientist, "title": "Data science offer", "body": "I got the offer for a data scientist role."},
            {"id": "swe", "url": engineer, "title": "Software offer", "body": "I got the offer as a software engineer."},
        ],
        role_key="data analyst",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    store_items(
        session,
        InterviewPlatform.youtube,
        [
            {"id": "coach1", "url": coach, "title": "Coach", "text": "I coach people to get the offer."},
            {"id": "tips1", "url": tips, "title": "Interview tips", "text": "Here are five ways to prepare for any interview."},
        ],
        role_key="data analyst",
        started_at=DAY_1,
        finished_at=DAY_1,
        max_snippet=2000,
    )
    rows = list(session.scalars(select(InterviewEvidence)))
    swe = read_stance("Data Analyst", "Software offer", "I got the offer as a software engineer.")
    assert swe.role_fit == "adjacent" and swe.counts_as_passed is False
    recruiter = read_stance(
        "Graduate Software Engineer",
        "Ex-Google Recruiter Explains: The Interview Secret to Getting Hired",
        "Book a 1:1",
    )
    assert recruiter.voice == "recruiter" and recruiter.counts_as_passed is False
    mentor = read_stance(
        "Graduate Software Engineer",
        "How to Pass Software Engineering Interviews Like the Top 1%",
        "Apply for a FREE discovery call. I help software engineers get hired.",
    )
    assert mentor.voice == "coach" and mentor.counts_as_passed is False
    titled = read_stance(
        "Graduate Software Engineer",
        "Ex-Google recruiter career coach on getting hired",
        "I am a recruiter.",
    )
    assert titled.voice == "coach"
    cached = read_stance(
        "company:acme:backend developer",
        "Offer",
        "I got the offer as a backend developer at Acme.",
    )
    assert cached.role_fit == "adjacent"
    pack = pack_from_payload_for_tests(
        session,
        role="Data Analyst",
        role_key="data analyst",
        payload={
            "summary": "The sources mix first-hand offers with other jobs and a coach.",
            "success_themes": [
                {"theme": "Take-home", "detail": 'The writer said "I got the offer for a data analyst role"', "cites": [passer]},
                {"theme": "Science", "detail": "A data scientist story", "cites": [scientist]},
                {"theme": "Coaching", "detail": "Book a call", "cites": [coach]},
            ],
            "failure_themes": [{"theme": "Generic", "detail": "a tip with no writer", "cites": [tips]}],
            "differentiators": [
                {"theme": "Neighbour", "detail": "another job", "cites": [scientist]},
                {"theme": "Seller", "detail": "a pitch", "cites": [coach]},
            ],
            "materials": [{"name": "Thread", "url": passer, "how_used": "read the offer"}],
            "practice_plan": [{"order": 1, "method": "Redo the take-home", "detail": "time it", "resource_urls": [passer]}],
        },
        rows=rows,
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(pack, InterviewIntelPack)
    assert [theme["theme"] for theme in pack.success_themes] == ["Take-home"]
    assert pack.success_themes[0]["detail"] == 'The writer said "I got the offer for a data analyst role"'
    assert "label" not in pack.success_themes[0]
    neighbour = next(theme for theme in pack.differentiators if theme["theme"] == "Neighbour")
    seller = next(theme for theme in pack.differentiators if theme["theme"] == "Seller")
    assert neighbour["label"] == "Adjacent role, not this role."
    assert neighbour["detail"] == "another job"
    assert seller["label"] == "Coach, not a person who passed."
    assert seller["detail"] == "a pitch"
    generic = next(theme for theme in pack.failure_themes if theme["theme"] == "Generic")
    assert generic["label"] == "Not a first-hand account."
    assert generic["detail"] == "a tip with no writer"

    invented = pack_from_payload_for_tests(
        session,
        role="Data Analyst",
        role_key="data analyst",
        payload={
            "summary": 'Someone wrote "this sentence was never in the post"',
            "success_themes": [],
            "failure_themes": [],
            "differentiators": [],
            "materials": [{"name": "Thread", "url": passer, "how_used": "read it"}],
            "practice_plan": [{"order": 1, "method": "Read", "detail": "the thread", "resource_urls": [passer]}],
        },
        rows=rows,
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(invented, list)
    assert any("quote is not in the sources" in problem for problem in invented)

    uncited = pack_from_payload_for_tests(
        session,
        role="Data Analyst",
        role_key="data analyst",
        payload={
            "summary": "A claim with nowhere to look.",
            "success_themes": [],
            "failure_themes": [{"theme": "Bare", "detail": "no link", "cites": []}],
            "differentiators": [],
            "materials": [{"name": "Thread", "url": passer, "how_used": "read it"}],
            "practice_plan": [{"order": 1, "method": "Read", "detail": "the thread", "resource_urls": [passer]}],
        },
        rows=rows,
        settings=_settings(),
        now=DAY_1,
    )
    assert isinstance(uncited, list)
    assert any("no evidence url" in problem for problem in uncited)


def test_only_the_writer_passing_counts() -> None:
    role = "Data Analyst"
    friend = read_stance(role, "Offer", "My friend got the offer for a data analyst role.")
    assert friend.counts_as_passed is False
    she = read_stance(role, "Offer", "She got the offer for a data analyst role.")
    assert she.counts_as_passed is False
    declined = read_stance(
        "Software Engineer",
        "Offer decision",
        "I passed on the offer after the software engineer interview.",
    )
    assert declined.counts_as_passed is False
    passed = read_stance(role, "Offer", "I got the offer for a data analyst role.")
    assert passed.counts_as_passed is True
    studied = read_stance(role, "Offer", "I studied every night and got the offer for a data analyst role.")
    assert studied.counts_as_passed is True
    own_work = read_stance(role, "Offer", "I finished my assignment and got the offer for a data analyst role.")
    assert own_work.counts_as_passed is True
    heard = read_stance(role, "Offer", "I was happy she got the offer for a data analyst role.")
    assert heard.counts_as_passed is False
    friend_story = read_stance(role, "Offer", "I heard my friend got the offer for a data analyst role.")
    assert friend_story.counts_as_passed is False
    tips = read_stance(role, "got the offer", "Here are five ways to prepare for any interview.")
    assert tips.counts_as_passed is False
    stored_tips = read_stance(role, "got the offer", "got the offer Here are five ways to prepare for any interview.")
    assert stored_tips.counts_as_passed is False
    stored_pass = read_stance(role, "got the offer", "got the offer I got the offer for a data analyst role.")
    assert stored_pass.counts_as_passed is True


def test_ordinary_engineering_is_not_a_course_seller() -> None:
    passed = read_stance(
        "Software Engineer",
        "Offer",
        "I use code daily. I got the offer as a software engineer.",
    )
    assert passed.voice == "first_hand"
    assert passed.counts_as_passed is True
    project = read_stance("Software Engineer", "Project", "I created a course project.")
    assert project.voice != "course_seller"
    assert project.counts_as_passed is False
    for pitch in (
        "Buy the course. I got the offer as a software engineer.",
        "Enroll now. I got the offer as a software engineer.",
        "Book a call. I got the offer as a software engineer.",
        "Use this discount code. I got the offer as a software engineer.",
    ):
        stance = read_stance("Software Engineer", "Offer", pitch)
        assert stance.voice == "course_seller"
        assert stance.counts_as_passed is False


def test_employer_match_is_a_whole_word() -> None:
    meta = read_stance(
        "Software Engineer",
        "Offer",
        "I got the offer as a software engineer working on metadata.",
        company="Meta",
    )
    assert meta.employer_match is False
    assert meta.counts_as_passed is False
    arm = read_stance(
        "Software Engineer",
        "Offer",
        "I got the offer as a software engineer and stayed warm.",
        company="Arm",
    )
    assert arm.employer_match is False
    assert arm.counts_as_passed is False
    acme = read_stance(
        "Software Engineer",
        "Interview",
        "I passed the Acme interview.",
        company="Acme",
    )
    assert acme.employer_match is True
    hired = read_stance(
        "Software Engineer",
        "Offer",
        "I passed the Acme software engineer interview.",
        company="Acme",
    )
    assert hired.employer_match is True
    assert hired.counts_as_passed is True


def _pack(session: Session, *, version: str, ready_at: datetime, expires_at: datetime, summary: str) -> InterviewIntelPack:
    pack = InterviewIntelPack(
        role_key="backend developer",
        role_label="Backend Developer",
        summary=summary,
        source_hash=summary,
        model="test",
        prompt_version=version,
        ready_at=ready_at,
        expires_at=expires_at,
    )
    session.add(pack)
    session.flush()
    return pack


def test_old_prompt_pack_is_not_served(session: Session) -> None:
    old = _pack(
        session,
        version="interview-plan-v1",
        ready_at=DAY_1,
        expires_at=DAY_1 + timedelta(days=7),
        summary="old",
    )
    assert fresh_pack(session, "backend developer", now=DAY_1) is None
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    queued = enqueue(session, applicant_id=applicant, role="Backend Developer", now=DAY_1)
    assert queued.status == InterviewRequestStatus.queued.value
    assert queued.pack_id is None
    assert old.prompt_version == "interview-plan-v1"

    current = _pack(
        session,
        version=PROMPT_VERSION,
        ready_at=DAY_1 - timedelta(days=1),
        expires_at=DAY_1 + timedelta(days=7),
        summary="current",
    )
    assert fresh_pack(session, "backend developer", now=DAY_1) is not None
    assert fresh_pack(session, "backend developer", now=DAY_1).id == current.id
    other = _applicant(session, roles=["Backend Developer"], locations=["Leeds"])
    ready = enqueue(session, applicant_id=other, role="Backend Developer", now=DAY_1)
    assert ready.status == InterviewRequestStatus.ready.value
    assert ready.pack_id == current.id

    current.expires_at = DAY_1
    session.flush()
    assert fresh_pack(session, "backend developer", now=DAY_1) is None


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
            "harshmaur/reddit-search-scraper": [_reddit_item(1, text="Got the offer after mock interviews.")],
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
    runs = [req for req in apify.requests if req.method == "POST" and req.url.path.endswith("/runs")]
    actors = [req.url.path for req in runs]
    assert any("harshmaur~reddit-search-scraper" in path for path in actors)
    assert any("streamers~youtube-scraper" in path for path in actors)
    bodies = {req.url.path: json.loads(req.content) for req in runs}
    reddit = next(body for path, body in bodies.items() if "harshmaur" in path)
    linkedin = next(body for path, body in bodies.items() if "linkedin-posts-search" in path)
    assert reddit["searchTerms"] == queries_for("Backend Developer")
    assert reddit["crawlCommentsPerPost"] is False
    assert reddit["searchComments"] is False
    assert "searches" not in reddit
    assert linkedin["keyword"] == queries_for("Backend Developer")[0]
    assert "searchQueries" not in linkedin


async def test_collect_stays_queued_when_budget_is_zero(session: Session) -> None:
    _policy(session, money_cap="0")
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    request = enqueue(session, applicant_id=applicant, role="Backend Developer", now=DAY_1)
    assert request.status == InterviewRequestStatus.queued.value

    apify = _MultiApify({"harshmaur/reddit-search-scraper": [_reddit_item(1, text="x")]})
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

    apify = _MultiApify({"harshmaur/reddit-search-scraper": [_reddit_item(99, text="should not run")]})
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
        now=datetime.now(UTC),
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


def test_pass_requires_affirmative_outcome_for_same_role_and_employer() -> None:
    for text in (
        "I never got an offer after my Data Analyst interview.",
        "If I got an offer as a Data Analyst I would celebrate.",
        "I got an offer as a Software Engineer. Now I want to become a Data Analyst.",
        "I passed my driving test. I am preparing for a Data Analyst interview.",
        "I got an offer as a Software Engineer while preparing for Data Analyst interviews.",
    ):
        assert not read_stance("Data Analyst", "Interview", text).counts_as_passed
    assert not read_stance(
        "Data Analyst", "Acme", "I got an offer as a Data Analyst at Elsewhere. Acme rejected me.", company="Acme"
    ).counts_as_passed
    assert read_stance("Data Analyst", "Offer", "I passed the Data Analyst interview.").counts_as_passed


def test_quotes_in_every_pack_field_require_source_text() -> None:
    url = "https://example.com/story"
    for section, field, links in (
        ("success_themes", "theme", {"cites": [url]}),
        ("materials", "name", {"url": url}),
        ("materials", "how_used", {"url": url}),
        ("practice_plan", "method", {"resource_urls": [url]}),
        ("practice_plan", "detail", {"resource_urls": [url]}),
    ):
        for quote in ('"invented"', "“invented”", "‘invented’"):
            problems = ground_pack(
                {"summary": "Source overview", section: [{field: quote, **links}]},
                {url}, {url: "Actual source text"},
            )
            assert any("quote is not in" in problem for problem in problems)
