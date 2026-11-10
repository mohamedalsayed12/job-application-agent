"""Step 21: one checked summary per job, written on first view. T35 and T37."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.matching.match import match_all
from app.models import (
    BudgetPolicy,
    Job,
    JobFreshness,
    JobSummary,
    SpendKind,
    SpendLedger,
    SummaryStatus,
)
from app.summaries.check import unsupported
from app.summaries.generate import (
    Reason,
    posting_text,
    source_hash,
    summarise_matches,
    summary_for_view,
)
from tests.test_ai_extraction import _public_resolve
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import SOON, _applicant, _gh

POSTING = (
    "Junior Backend Developer at Acme in Manchester. You will build payment services in Python and "
    "PostgreSQL with a team of 12 engineers. Salary £36,500. Applications close on 3 March 2027."
)
GOOD = "A junior backend role building payment services in Python and PostgreSQL. You join a team of 12 engineers."
PASSWORD = "correct horse battery"


@pytest.mark.parametrize(
    ("summary", "problems"),
    [
        (GOOD, []),
        ("The salary is £36500 and you work in Manchester.", []),
        ("The salary is £40,000.", ["40,000"]),
        ("Applications close in April.", ["April"]),
        ("Applications close on 3 March 2027.", []),
        ("You will work with Google and Kubernetes.", ["Google", "Kubernetes"]),
        ("You may need three years of Python.", ["three"]),
        ("Acme's team builds payment services.", []),
        ("Build payment services for Acme.", []),
        ("Leading payment services at Acme.", ["Leading"]),
        ("Key skills are Python and PostgreSQL. Required: Python.", []),
        ("A Manchester-based role building Python-based payment services.", []),
        ("A Leeds-based role.", ["Leeds-based"]),
        ("You build a payment service in Python.", []),
        ("You build payment APIs.", ["APIs"]),
        ("You join a team of twelve engineers.", []),
        ("You join a team of twelve developers.", ["twelve"]),
        ("Applications close on 3 March, three days away.", ["three"]),
    ],
)
def test_t35_a_number_date_or_name_not_in_the_posting_is_caught(summary: str, problems: list[str]) -> None:
    assert unsupported(summary, POSTING) == problems


@pytest.mark.parametrize(
    ("posting", "summary", "problems"),
    [
        ("Based in the United Kingdom.", "A UK role.", []),
        ("A UK role.", "Based in the United Kingdom.", []),
        ("You will own continuous integration and deployment.", "You own CI pipelines.", []),
        ("Trade foreign exchange products.", "You build FX products.", []),
        ("Run services on K8s.", "You run Kubernetes services.", []),
        ("Come and join us in London.", "Join the United States team.", ["United", "States"]),
        ("A UK role.", "A role in Taiwan.", ["Taiwan"]),
        ("Strong English-language skills and Git-based workflows.", "You need English and Git.", []),
        ("Datastores, especially Mongo.", "You work with MongoDB.", []),
    ],
)
def test_d47_names_the_posting_writes_another_way_are_accepted(posting: str, summary: str, problems: list[str]) -> None:
    assert unsupported(summary, posting) == problems


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        ai_provider="anthropic",
        anthropic_api_key="test-anthropic-key",
        ai_summary_unit_price=Decimal("0.001"),
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _Provider:
    """Answers like the Messages API and counts the calls."""

    def __init__(self, summary: str = GOOD, *, status: int = 200) -> None:
        self.summary, self.status, self.calls = summary, status, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        assert "<JOB_POSTING_DATA>" in body["messages"][0]["content"]
        return httpx.Response(
            self.status,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps({"summary": self.summary})}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 400, "output_tokens": 60},
            },
        )

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.anthropic.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )


def _policy(session: Session, money_cap: Decimal) -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=money_cap,
            request_cap=None,
            currency="GBP",
            effective_from=datetime.now(UTC) - timedelta(days=1),
            effective_to=None,
        )
    )
    session.flush()


async def _open_job(session: Session, content: str = POSTING) -> Job:
    _board(session)
    await _Collector(session).read(200, {"jobs": [_gh(1, content=content)]})
    return _job(session, 1)


async def _view(session: Session, job: Job, provider: _Provider, **settings: object):
    return await summary_for_view(
        session, job, gateway=provider.gateway(), settings=_settings(**settings), persist=session.flush
    )


async def test_t35_the_first_view_writes_a_summary_that_every_later_view_reuses(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider()

    first = await _view(session, job, provider)
    again = await _view(session, job, provider)

    assert (first.summary, first.reason) == (GOOD, Reason.summary)
    assert again.summary == GOOD
    assert provider.calls == 1
    row = session.scalar(select(JobSummary).where(JobSummary.job_id == job.id))
    assert (row.status, row.prompt_tokens, row.completion_tokens) == (SummaryStatus.accepted, 400, 60)
    ledger = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_summary)).all()
    assert [(entry.actual_cost, entry.related_item_id) for entry in ledger] == [(Decimal("0.001"), job.id)]


async def test_t35_a_summary_with_an_unsupported_number_is_rejected_and_not_retried(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider("A junior backend role paying £90,000.")

    first = await _view(session, job, provider)
    again = await _view(session, job, provider)

    assert (first.summary, first.reason) == (None, Reason.rejected)
    assert again.reason == Reason.rejected
    assert provider.calls == 1
    row = session.scalar(select(JobSummary).where(JobSummary.job_id == job.id))
    assert row.unsupported == ["90,000"]


async def test_a_changed_posting_gets_a_new_summary(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider()
    await _view(session, job, provider)
    job.description = POSTING + " You will also use Docker."
    session.flush()
    await _view(session, job, provider)
    assert provider.calls == 2
    assert session.scalar(select(func.count()).select_from(JobSummary)) == 1


async def test_t37_with_no_budget_nothing_is_called_or_saved(session: Session) -> None:
    job = await _open_job(session)
    provider = _Provider()
    shown = await _view(session, job, provider)
    assert (shown.summary, shown.reason) == (None, Reason.budget)
    assert provider.calls == 0
    assert session.scalar(select(func.count()).select_from(JobSummary)) == 0


async def test_token_caps_stop_the_call(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider()
    assert (await _view(session, job, provider, ai_summary_per_item_token_cap=100)).reason == Reason.token_cap
    assert (await _view(session, job, provider, ai_summary_daily_token_cap=100)).reason == Reason.token_cap
    assert provider.calls == 0


async def test_a_provider_failure_saves_nothing_and_costs_nothing(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider(status=500)
    shown = await _view(session, job, provider)
    assert shown.reason == Reason.provider_failed
    assert session.scalar(select(func.count()).select_from(JobSummary)) == 0
    assert session.scalar(select(SpendLedger.actual_cost)) == Decimal("0")


async def test_a_view_while_another_is_writing_waits_instead_of_paying_twice(session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    provider = _Provider()
    session.add(
        JobSummary(
            job_id=job.id,
            status=SummaryStatus.pending,
            source_hash=source_hash(posting_text(job)),
            model="m",
            prompt_version="v",
            updated_at=datetime.now(UTC),
        )
    )
    session.flush()
    assert (await _view(session, job, provider)).reason == Reason.in_progress
    assert provider.calls == 0


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    get_settings.cache_clear()
    throttle.reset()
    provider = _Provider()

    def _override_db() -> Iterator[Session]:
        yield session

    async def _override_gateway() -> AsyncIterator[FetchGateway]:
        gateway = provider.gateway()
        yield gateway
        await gateway.aclose()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[get_gateway] = _override_gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        test_client.provider = provider  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _login(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": "v@example.com", "password": PASSWORD})).status_code == 303


async def test_the_job_page_needs_a_login_and_shows_the_checked_summary(client: httpx.AsyncClient, session: Session) -> None:
    _policy(session, Decimal("5"))
    job = await _open_job(session)
    session.commit()
    assert (await client.get(f"/jobs/{job.id}")).status_code == 303
    await _login(client)
    page = (await client.get(f"/jobs/{job.id}")).text
    assert GOOD in page
    assert "checked against it" in page
    assert 'href="https://job-boards.greenhouse.io/acme/jobs/1"' in page


async def test_t37_with_the_budget_used_up_the_job_page_shows_the_description(
    client: httpx.AsyncClient, session: Session
) -> None:
    job = await _open_job(session)
    session.commit()
    await _login(client)
    response = await client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    assert "build payment services in Python" in response.text
    assert client.provider.calls == 0  # type: ignore[attr-defined]


async def test_a_job_that_is_not_open_is_not_shown(client: httpx.AsyncClient, session: Session) -> None:
    job = await _open_job(session)
    job.freshness = JobFreshness.closed
    session.commit()
    await _login(client)
    assert (await client.get(f"/jobs/{job.id}")).status_code == 404


async def _matched_jobs(session: Session) -> tuple[Job, Job, Job]:
    """A Manchester job, the same job in London, which scores lower, and a job nobody matches."""
    _board(session)
    jobs = [
        _gh(1, title="Backend Developer", city="Manchester, UK", content=POSTING),
        _gh(2, title="Backend Developer", city="London, UK", content=POSTING),
        _gh(3, title="Sales Manager", city="Manchester, UK", content=POSTING),
    ]
    await _Collector(session).read(200, {"jobs": jobs})
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)
    return _job(session, 1), _job(session, 2), _job(session, 3)


async def _batch(session: Session, provider: _Provider, **settings: object):
    return await summarise_matches(
        session, gateway=provider.gateway(), settings=_settings(**settings), persist=session.flush
    )


def _summarised(session: Session) -> set:
    return set(session.scalars(select(JobSummary.job_id).where(JobSummary.status == SummaryStatus.accepted)))


async def test_d47_matched_jobs_are_summarised_ahead_of_time_best_match_first(session: Session) -> None:
    _policy(session, Decimal("0.0015"))
    best, other, unmatched = await _matched_jobs(session)
    provider = _Provider()

    report = await _batch(session, provider)

    assert report.stopped_by == "budget"
    assert _summarised(session) == {best.id}

    _policy(session, Decimal("5"))
    report = await _batch(session, provider)
    assert report.stopped_by is None
    assert _summarised(session) == {best.id, other.id}
    assert unmatched.id not in _summarised(session)
    assert provider.calls == 2


async def test_d47_a_second_batch_pays_for_nothing_already_summarised(session: Session) -> None:
    _policy(session, Decimal("5"))
    await _matched_jobs(session)
    provider = _Provider()
    await _batch(session, provider)
    await _batch(session, provider)
    assert provider.calls == 2


async def test_d47_the_batch_stops_at_the_days_token_cap(session: Session) -> None:
    _policy(session, Decimal("5"))
    await _matched_jobs(session)
    provider = _Provider()
    report = await _batch(session, provider, ai_summary_daily_token_cap=100)
    assert (report.stopped_by, provider.calls) == ("daily token cap", 0)


async def test_d47_without_an_api_key_the_batch_does_nothing(session: Session) -> None:
    _policy(session, Decimal("5"))
    await _matched_jobs(session)
    provider = _Provider()
    report = await _batch(session, provider, anthropic_api_key="")
    assert (report.stopped_by, provider.calls) == ("not_configured", 0)
