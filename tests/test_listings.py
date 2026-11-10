"""Step 22: the listings page. T36 and T37."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.listings import Listing, render_listing, salary_text
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.matching.semantic import embed_matches, vectors_for
from app.models import (
    ApplicantJobMatch,
    Job,
    JobEmbedding,
    JobFreshness,
    JobSummary,
    SummaryStatus,
)
from app.skills.taxonomy import load_taxonomy, map_job_skills
from tests.conftest import WordEmbedder
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import _gh

PASSWORD = "correct horse battery"
ADVERT = "Build payment services in Python and PostgreSQL for our platform team. " * 6


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


async def _jobs(session: Session) -> None:
    _board(session)
    await _Collector(session).read(
        200,
        {
            "jobs": [
                _gh(1, title="Junior Backend Developer", city="Manchester, UK", content=ADVERT),
                _gh(2, title="Graduate Backend Developer", city="Manchester, UK", content=ADVERT),
                _gh(3, title="Backend Developer, Early Careers", city="London, UK", content=ADVERT),
                _gh(4, title="Sales Manager", city="Manchester, UK", content=ADVERT),
                _gh(5, title="Backend Developer", city="Manchester, UK", content=ADVERT),
            ]
        },
    )
    load_taxonomy(session)
    map_job_skills(session)
    session.commit()


async def _register_with_profile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": "l@example.com", "password": PASSWORD})).status_code == 303
    profile = {
        "target_roles": "Junior Backend Developer",
        "locations": "Manchester",
        "experience_level": "entry",
        "skills": "Python",
    }
    assert (await client.post("/profile", data=profile)).status_code == 303


def _titles(page: str) -> list[str]:
    return re.findall(r'<h2><a href="/jobs/[0-9a-f-]+">([^<]+)</a></h2>', page)


async def test_the_listings_need_a_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/listings")).status_code == 303


async def test_saving_a_profile_matches_at_once_and_lists_the_best_first(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings", params={"sort": "score"})).text
    assert sorted(_titles(page)) == ["Graduate Backend Developer", "Junior Backend Developer"]
    # D53: only the AI's score out of 5 is shown; jobs it has not scored yet say so.
    assert "/100" not in page
    assert page.count("Not scored yet") == 2
    assert "Sales Manager" not in page

    everywhere = _titles((await client.get("/listings", params={"everywhere": "1", "sort": "score"})).text)
    assert sorted(everywhere[:2]) == ["Graduate Backend Developer", "Junior Backend Developer"]
    assert everywhere[2:] == ["Backend Developer, Early Careers"]


async def test_d46_an_entry_level_seeker_sees_only_jobs_shown_to_be_entry_level(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    everywhere = _titles((await client.get("/listings", params={"everywhere": "1"})).text)
    assert "Backend Developer" not in everywhere


async def test_t36_each_listing_shows_its_source_apply_link_and_reasons(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings")).text
    assert "Source: greenhouse." in page
    assert 'href="https://job-boards.greenhouse.io/acme/jobs/1"' in page
    assert "Salary: Not stated</span>" in page
    assert "Location: in Manchester" in page
    assert "Skills you have: Python" in page
    assert "Last confirmed open" in page


async def test_t36_a_job_that_stops_being_open_leaves_the_listings(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    for freshness in (JobFreshness.closed, JobFreshness.unknown):
        _job(session, 1).freshness = freshness
        session.commit()
        assert "Junior Backend Developer" not in _titles((await client.get("/listings")).text)
    assert session.scalar(select(ApplicantJobMatch).where(ApplicantJobMatch.job_id == _job(session, 1).id))


async def test_t37_without_a_summary_the_listing_shows_the_description(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings")).text
    assert "<details><summary>Description</summary>" in page
    assert "Build payment services in Python" in page

    session.add(
        JobSummary(
            job_id=_job(session, 1).id,
            status=SummaryStatus.accepted,
            summary_text="A junior role building payment services in Python.",
            source_hash="h",
            model="m",
            prompt_version="v",
        )
    )
    session.add(
        JobSummary(
            job_id=_job(session, 2).id,
            status=SummaryStatus.rejected,
            summary_text="A role paying £90,000.",
            source_hash="h",
            model="m",
            prompt_version="v",
        )
    )
    session.commit()
    page = (await client.get("/listings")).text
    assert "A junior role building payment services in Python." in page
    assert "£90,000" not in page


def _listing(source: str, **job: object) -> Listing:
    return Listing(
        job_id=uuid.uuid4(),
        title="Backend Developer",
        company="Acme",
        places="Manchester",
        salary="Not stated",
        summary=None,
        description="Text.",
        score=Decimal("0.8"),
        reasons={"role": {"kind": "exact", "target": "Backend Developer", "level": None}, "location": "in Manchester"},
        matched_skills=[],
        missing_skills=[],
        missing_licences=[],
        last_seen_at=datetime(2026, 11, 9, tzinfo=UTC),
        source=source,
        apply_url=str(job.get("apply_url", "https://example.com/apply")),
    )


def test_an_apply_link_that_is_not_a_web_link_is_not_rendered() -> None:
    page = render_listing(_listing("lever", apply_url="javascript:alert(1)"))
    assert "javascript:" not in page
    assert "No usable apply link" in page


@pytest.mark.parametrize(
    ("job", "text"),
    [
        (dict(salary_known=False), "Not stated"),
        (dict(salary_known=True, salary_min=Decimal(30000), salary_max=Decimal(40000), salary_currency="GBP", salary_period="year"), "£30,000 to £40,000 a year"),
        (dict(salary_known=True, salary_min=Decimal(25), salary_max=Decimal(25), salary_currency="GBP", salary_period="hour"), "£25 an hour"),
    ],
)
def test_salary_text(job: dict, text: str) -> None:
    defaults = dict(salary_min=None, salary_max=None, salary_currency=None, salary_period=None)
    assert salary_text(Job(**{**defaults, **job})) == text


def _summarise(session: Session, number: int, text: str) -> None:
    session.add(
        JobSummary(
            job_id=_job(session, number).id,
            status=SummaryStatus.accepted,
            summary_text=text,
            source_hash="h",
            model="m",
            prompt_version="v",
        )
    )
    session.commit()


async def test_d48_a_search_puts_the_jobs_closest_in_meaning_first(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    _summarise(session, 1, "Build the game engine and rendering tools for console games.")
    _summarise(session, 2, "Build ledger and card payment services for a fintech bank.")
    await _register_with_profile(client)

    for words, first in (("fintech payments", "Graduate Backend Developer"), ("games", "Junior Backend Developer")):
        page = (await client.get("/listings", params={"q": words})).text
        assert _titles(page)[0] == first
        assert f'value="{words}"' in page
        assert "Closest in meaning to" in page
    # A search orders the person's matches; it never adds a job the filters leave out.
    assert "Backend Developer, Early Careers" not in _titles((await client.get("/listings", params={"q": "games"})).text)


async def test_d48_a_vector_is_made_once_and_again_when_the_job_text_changes(session: Session) -> None:
    await _jobs(session)
    calls: list[int] = []

    class Counting(WordEmbedder):
        def passages(self, texts: list[str]) -> list[list[float]]:
            calls.append(len(texts))
            return super().passages(texts)

    jobs = [_job(session, 1), _job(session, 2)]
    vectors_for(session, jobs, Counting())
    vectors_for(session, jobs, Counting())
    assert calls == [2]

    _summarise(session, 1, "Build the game engine.")
    vectors_for(session, jobs, Counting())
    assert calls == [2, 1]
    assert session.scalar(select(func.count()).select_from(JobEmbedding)) == 2


async def test_d48_the_worker_makes_vectors_only_for_open_matched_jobs(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    matched = set(session.scalars(select(ApplicantJobMatch.job_id)))
    _job(session, 1).freshness = JobFreshness.closed
    session.commit()

    embed_matches(session, WordEmbedder())
    made = set(session.scalars(select(JobEmbedding.job_id)))
    assert made == matched - {_job(session, 1).id}
    assert made


async def test_the_listings_show_the_newest_jobs_first_by_default(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings")).text

    assert "Sorted by newest." in page
    assert '<option value="newest" selected>Newest first</option>' in page
    newest = (await client.get("/listings", params={"sort": "newest"})).text
    assert _titles(page) == _titles(newest)

