"""Step 22: the listings page. T36 and T37."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.listings import Listing, render_listing, salary_text
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import (
    ApplicantJobMatch,
    Job,
    JobFreshness,
)
from app.skills.taxonomy import load_taxonomy, map_job_skills
from tests.auth import sign_up
from tests.jobs import _job, _store
from tests.test_matching import _posting

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
    _store(
        session,
        [
        _posting(1, title="Junior Backend Developer", city="Manchester, UK", content=ADVERT),
        _posting(2, title="Graduate Backend Developer", city="Manchester, UK", content=ADVERT),
        _posting(3, title="Backend Developer, Early Careers", city="London, UK", content=ADVERT),
        _posting(4, title="Sales Manager", city="Manchester, UK", content=ADVERT),
        _posting(5, title="Backend Developer", city="Manchester, UK", content=ADVERT),
        ],
    )
    load_taxonomy(session)
    map_job_skills(session)
    session.commit()


async def _register_with_profile(client: httpx.AsyncClient) -> None:
    assert (await sign_up(client, "l@example.com", PASSWORD)).status_code == 303
    profile = {
        "target_roles": "Junior Backend Developer",
        "locations": "Manchester",
        "experience_levels": "entry",
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
    page = (await client.get("/listings", params={"sort": "score", "everywhere": "0"})).text
    assert sorted(_titles(page)) == ["Graduate Backend Developer", "Junior Backend Developer"]
    # D53: only the AI's score out of 5 is shown; jobs it has not scored yet say so.
    assert "/100" not in page
    assert page.count("Not scored yet") == 2
    assert "Sales Manager" not in page

    # D72: with no location filter chosen, every match shows, the ones outside the person's locations last.
    everywhere = _titles((await client.get("/listings", params={"sort": "score"})).text)
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
    assert "Source: linkedin." in page
    assert 'href="https://acme.example/careers/1"' in page
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


async def test_t37_the_listing_shows_the_description(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings")).text
    assert "<details><summary>Description</summary>" in page
    assert "Build payment services in Python" in page
    assert "AI summary" not in page
    assert 'name="q"' not in page


def _listing(source: str, **job: object) -> Listing:
    return Listing(
        job_id=uuid.uuid4(),
        title="Backend Developer",
        company="Acme",
        places="Manchester",
        salary="Not stated",
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
    page = render_listing(_listing("linkedin", apply_url="javascript:alert(1)"))
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


async def test_the_listings_show_the_newest_jobs_first_by_default(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    page = (await client.get("/listings")).text

    assert "Sorted by newest." in page
    assert '<option value="newest" selected>Newest first</option>' in page
    newest = (await client.get("/listings", params={"sort": "newest"})).text
    assert _titles(page) == _titles(newest)



async def test_d72_page_links_keep_the_my_locations_filter(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    mine = (await client.get("/listings", params={"everywhere": "0"})).text
    assert '<option value="0" selected>My locations</option>' in mine
    assert "everywhere=1" not in (await client.get("/listings")).text


async def test_the_sort_choice_is_remembered_until_changed(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)

    chosen = (await client.get("/listings", params={"sort": "score"})).text
    assert "Sorted by AI score." in chosen
    assert "Sorted by AI score." in (await client.get("/listings")).text

    assert "Sorted by newest." in (await client.get("/listings", params={"sort": "newest"})).text
    assert "Sorted by newest." in (await client.get("/listings")).text




async def test_one_persons_sort_choice_does_not_reach_another(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    await client.get("/listings", params={"sort": "score"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as other:
        assert (await sign_up(other, "o@example.com", PASSWORD)).status_code == 303
        profile = {"target_roles": "Junior Backend Developer", "locations": "Manchester", "experience_levels": "entry", "skills": "Python"}
        assert (await other.post("/profile", data=profile)).status_code == 303
        assert "Sorted by newest." in (await other.get("/listings")).text
    assert "Sorted by AI score." in (await client.get("/listings")).text


def test_the_pager_keeps_the_sort_choice() -> None:
    from app.api.listings import _nav

    nav = _nav(2, 80, everywhere=False, role="", sort="score", show="roles")
    assert nav.count("sort=score") == 2
    # newest is the default, so pager links omit it (D72 query defaults).
    assert "sort=" not in _nav(2, 80, everywhere=False, role="", sort="newest", show="roles")


async def test_the_job_page_needs_a_login_and_shows_the_description(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    job = _job(session, 1)
    assert (await client.get(f"/jobs/{job.id}")).status_code == 303
    await _register_with_profile(client)
    page = (await client.get(f"/jobs/{job.id}")).text
    assert "Build payment services in Python" in page
    assert "checked against it" not in page
    assert "AI summary" not in page


async def test_a_closed_job_is_not_shown_on_its_page(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register_with_profile(client)
    job = _job(session, 1)
    job.freshness = JobFreshness.closed
    session.commit()
    assert (await client.get(f"/jobs/{job.id}")).status_code == 404

