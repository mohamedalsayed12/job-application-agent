"""D44: jobs outside the target roles that the applicant's skills fit."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.matching.match import RoleKind, SkillFit, match_by_skills
from app.models import ExperienceLevel
from app.skills.taxonomy import load_taxonomy, map_job_skills
from tests.test_cleaning import _board, _Collector
from tests.test_matching import _gh

PASSWORD = "correct horse battery"
DATA_ADVERT = "Build data pipelines in Python with PostgreSQL, Docker and SQL for our analytics team. " * 6
SALES_ADVERT = "Grow our accounts across the north west and close new business with retailers. " * 6
FOUR = ["Python", "PostgreSQL", "Docker", "SQL"]


def _fit(matched: int, missing: int) -> SkillFit:
    return SkillFit([f"s{n}" for n in range(matched)], [f"m{n}" for n in range(missing)], on_profile=True)


@pytest.mark.parametrize(
    ("title", "fit", "level", "expected"),
    [
        ("Data Engineer", _fit(3, 1), ExperienceLevel.entry, "level not stated"),
        ("Graduate Data Engineer", _fit(3, 0), ExperienceLevel.entry, "entry level in the title"),
        ("Data Engineer", _fit(2, 0), ExperienceLevel.entry, None),
        ("Data Engineer", _fit(3, 4), ExperienceLevel.entry, None),
        ("Senior Data Engineer", _fit(5, 0), ExperienceLevel.entry, None),
        ("Data Engineering Intern", _fit(4, 0), ExperienceLevel.entry, None),
        ("Senior Data Engineer", _fit(5, 0), ExperienceLevel.senior, ""),
    ],
)
def test_d44_a_job_is_found_by_skills_only_when_most_of_its_skills_are_held(
    title: str, fit: SkillFit, level: str, expected: str | None
) -> None:
    """`expected` is the level shown, "" for a match with no level, None for no match."""
    found = match_by_skills(title, "Work on our data platform.", fit, level=level)
    if expected is None:
        assert found is None
        return
    assert found is not None
    assert (found.kind, found.level or "") == (RoleKind.skills, expected)


def test_d44_years_of_experience_rule_out_an_entry_seeker() -> None:
    advert = "You will need 5+ years of experience with Python."
    assert match_by_skills("Data Engineer", advert, _fit(4, 0), level=ExperienceLevel.entry) is None


def test_d44_someone_after_an_internship_sees_only_internships() -> None:
    assert match_by_skills("Data Engineer", "", _fit(4, 0), level=ExperienceLevel.internship) is None
    found = match_by_skills("Data Engineering Intern", "", _fit(4, 0), level=ExperienceLevel.internship)
    assert found is not None and found.kind == RoleKind.skills


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
                _gh(1, title="Junior Backend Developer", city="Manchester, UK", content=DATA_ADVERT),
                _gh(2, title="Data Engineer", city="Manchester, UK", content=DATA_ADVERT),
                _gh(3, title="Account Manager", city="Manchester, UK", content=SALES_ADVERT),
            ]
        },
    )
    load_taxonomy(session)
    map_job_skills(session)
    session.commit()


async def _register(client: httpx.AsyncClient, skills: list[str]) -> None:
    assert (await client.post("/register", data={"email": "d@example.com", "password": PASSWORD})).status_code == 303
    profile = {
        "target_roles": "Junior Backend Developer",
        "locations": "Manchester",
        "experience_level": "entry",
        "skills": "\n".join(skills),
    }
    assert (await client.post("/profile", data=profile)).status_code == 303


def _titles(page: str) -> list[str]:
    return re.findall(r'<h2><a href="/jobs/[0-9a-f-]+">([^<]+)</a></h2>', page)


async def test_d44_the_listings_point_to_jobs_the_skills_fit(client: httpx.AsyncClient, session: Session) -> None:
    await _jobs(session)
    await _register(client, FOUR)

    roles = (await client.get("/listings")).text
    assert _titles(roles) == ["Junior Backend Developer"]
    assert "1 more job fits your skills</b>" in roles
    assert 'href="/listings?show=skills"' in roles

    skills = (await client.get("/listings", params={"show": "skills"})).text
    assert _titles(skills) == ["Data Engineer"]
    assert "Fits your skills</span>" in skills
    assert "Role: not one of your target roles, but you have 4 of the 4 skills it names" in skills
    assert "more job" not in skills

    both = (await client.get("/listings", params={"show": "all"})).text
    assert sorted(_titles(both)) == ["Data Engineer", "Junior Backend Developer"]
    assert "Account Manager" not in both


async def test_d44_without_skills_the_listings_say_how_to_discover_more(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _jobs(session)
    await _register(client, [])
    page = (await client.get("/listings")).text
    assert "Add your skills on" in page
    assert _titles(page) == ["Junior Backend Developer"]
