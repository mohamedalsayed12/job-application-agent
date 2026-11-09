"""Step 19: profile skills and job skills land on the same taxonomy entries. T33 and A12."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import ApplicantSkill, JobSkill, Skill, SkillMatchMethod, SkillSource
from app.skills.local_skills import LOCAL_SKILLS
from app.skills.mapping import LabelEntry, find_in_text
from app.skills.taxonomy import (
    coverage_report,
    label_index,
    load_taxonomy,
    map_job_skills,
    read_esco_file,
    skill_for_text,
    suggest_skills,
)
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import _gh

PASSWORD = "correct horse battery"


@pytest.fixture
def loaded(session: Session) -> dict[str, LabelEntry]:
    load_taxonomy(session)
    return label_index(session)


def _preferred(session: Session, skill_id: uuid.UUID) -> str:
    return session.get(Skill, skill_id).preferred_label


def _found(session: Session, index: dict[str, LabelEntry], text: str) -> set[str]:
    return {_preferred(session, hit.skill_id) for hit in find_in_text(text, index)}


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


def test_the_esco_file_holds_the_technical_skills_with_their_other_names() -> None:
    entries = read_esco_file()
    assert len(entries) > 800
    assert len({entry.taxonomy_id for entry in entries}) == len(entries)
    assert all(entry.taxonomy_id.startswith("http://data.europa.eu/esco/skill/") for entry in entries)
    postgres = next(entry for entry in entries if entry.preferred == "PostgreSQL")
    assert "Postgres" in postgres.alternatives


def test_our_own_skills_are_marked_as_ours(session: Session, loaded: dict[str, LabelEntry]) -> None:
    ours = session.scalars(select(Skill).where(Skill.source == SkillSource.local)).all()
    assert {skill.preferred_label for skill in ours} == set(LOCAL_SKILLS)
    assert all(skill.taxonomy_id.startswith("local:") for skill in ours)
    assert session.scalar(select(func.count()).select_from(Skill).where(Skill.source == SkillSource.esco)) > 800


def test_loading_twice_changes_nothing(session: Session, loaded: dict[str, LabelEntry]) -> None:
    before = {skill.taxonomy_id: skill.id for skill in session.scalars(select(Skill))}
    load_taxonomy(session)
    assert {skill.taxonomy_id: skill.id for skill in session.scalars(select(Skill))} == before


@pytest.mark.parametrize(
    ("typed", "written"),
    [
        ("Postgres", "PostgreSQL"),
        ("postgresql", "Postgres"),
        ("Python", "Python"),
        ("K8s", "Kubernetes"),
        ("AWS", "Amazon Web Services"),
        ("Golang", "Golang"),
    ],
)
def test_t33_a_profile_skill_and_a_job_skill_reach_the_same_entry(
    loaded: dict[str, LabelEntry], typed: str, written: str
) -> None:
    from_profile = skill_for_text(loaded, typed)
    from_job = find_in_text(f"You will work with {written} every day.", loaded)
    assert from_profile is not None
    assert [hit.skill_id for hit in from_job] == [from_profile]


@pytest.mark.parametrize(
    "text",
    [
        "You react quickly to change and go the extra mile.",
        "We excel at access to computer chips and rest on Sunday.",
        "salt and a chef in the less busy canvas of R&D.",
        "Excel at stakeholder management.",
        "A swift, rusty flask of express post.",
        "Go to market with Google.",
        "A degree in Computer Science. Guard rails for our IoT customers.",
    ],
)
def test_ordinary_words_in_an_advert_are_not_skills(loaded: dict[str, LabelEntry], text: str) -> None:
    assert find_in_text(text, loaded) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Experience with React and Rust.", {"React", "Rust"}),
        ("AWS/GCP, Docker, and Kubernetes.", {"Amazon Web Services", "Google Cloud", "Docker", "Kubernetes"}),
        ("Python-based services; Node.js.", {"Python (computer programming)", "Node.js"}),
        ("We use Spark for data.", {"Apache Spark"}),
        ("Strong CI/CD and .NET.", {"CI/CD", ".NET"}),
        ("C++, C# and Excel.", {"C++", "C#", "Microsoft Excel"}),
        ("Applied ML and Rails.", {"Machine Learning", "Ruby on Rails"}),
        ("Python, R, or MATLAB.", {"Python (computer programming)", "R", "MATLAB"}),
    ],
)
def test_real_skill_names_in_an_advert_are_found(
    session: Session, loaded: dict[str, LabelEntry], text: str, expected: set[str]
) -> None:
    assert _found(session, loaded, text) == expected


def test_a_longer_name_beats_the_shorter_one_inside_it(session: Session, loaded: dict[str, LabelEntry]) -> None:
    assert _found(session, loaded, "Built on Ruby on Rails.") == {"Ruby on Rails"}


def test_how_each_skill_matched_is_recorded(loaded: dict[str, LabelEntry]) -> None:
    by_text = {hit.matched_text: hit.how_matched for hit in find_in_text("PostgreSQL or Postgres, with Docker", loaded)}
    assert by_text["PostgreSQL"] == SkillMatchMethod.exact
    assert "Postgres" not in by_text
    other = find_in_text("Postgres", loaded)
    assert other[0].how_matched == SkillMatchMethod.alternative_label
    assert other[0].matched_text == "Postgres"


async def test_t33_job_skills_are_stored_and_a_repeat_changes_nothing(session: Session, loaded: dict[str, LabelEntry]) -> None:
    _board(session)
    await _Collector(session).read(200, {"jobs": [_gh(1, content="We run Postgres, Docker and Python. " * 20)]})
    assert map_job_skills(session) == 3
    job = _job(session, 1)
    rows = session.scalars(select(JobSkill).where(JobSkill.job_id == job.id)).all()
    assert {_preferred(session, row.skill_id) for row in rows} == {"PostgreSQL", "Docker", "Python (computer programming)"}
    postgres = next(row for row in rows if row.matched_text == "Postgres")
    assert postgres.how_matched == SkillMatchMethod.alternative_label

    ids = {row.id for row in rows}
    assert map_job_skills(session) == 3
    assert {row.id for row in session.scalars(select(JobSkill).where(JobSkill.job_id == job.id))} == ids


async def test_a_skill_no_longer_in_the_advert_is_removed(session: Session, loaded: dict[str, LabelEntry]) -> None:
    _board(session)
    await _Collector(session).read(200, {"jobs": [_gh(1, content="We run Docker and Python. " * 20)]})
    map_job_skills(session)
    _job(session, 1).description = "We run Docker only."
    session.flush()
    assert map_job_skills(session) == 1


async def test_a12_the_report_separates_esco_from_our_own_list(session: Session, loaded: dict[str, LabelEntry]) -> None:
    _board(session)
    jobs = [_gh(1, content="We run Postgres and Docker. " * 20), _gh(2, content="Nothing technical here at all. " * 20)]
    await _Collector(session).read(200, {"jobs": jobs})
    map_job_skills(session)
    report = coverage_report(session)
    assert report.open_jobs == 2
    assert report.jobs_with_skills == 1
    assert (report.mentions, report.esco_mentions, report.local_mentions) == (2, 1, 1)


async def _register(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": "p@example.com", "password": PASSWORD})).status_code == 303


async def test_t33_saving_a_profile_links_skills_and_keeps_what_was_typed(
    client: httpx.AsyncClient, session: Session, loaded: dict[str, LabelEntry]
) -> None:
    await _register(client)
    data = {
        "target_roles": "Backend Developer",
        "locations": "Manchester",
        "experience_level": "entry",
        "skills": "Postgres\nK8s\nKnitting patterns",
    }
    assert (await client.post("/profile", data=data)).status_code == 303
    rows = {row.text_as_typed: row for row in session.scalars(select(ApplicantSkill))}
    assert _preferred(session, rows["Postgres"].skill_id) == "PostgreSQL"
    assert _preferred(session, rows["K8s"].skill_id) == "Kubernetes"
    assert rows["Knitting patterns"].skill_id is None
    assert "Knitting patterns" in (await client.get("/profile")).text


async def test_skills_saved_before_the_list_was_loaded_are_linked_by_loading_it(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _register(client)
    data = {"target_roles": "Developer", "locations": "Leeds", "experience_level": "mid", "skills": "Postgres"}
    await client.post("/profile", data=data)
    assert session.scalar(select(ApplicantSkill)).skill_id is None
    load_taxonomy(session)
    assert _preferred(session, session.scalar(select(ApplicantSkill)).skill_id) == "PostgreSQL"


def test_suggestions_find_names_by_their_start_shortest_first(session: Session, loaded: dict[str, LabelEntry]) -> None:
    assert suggest_skills(session, "Postg")[:2] == ["Postgres", "PostgreSQL"]
    assert "Kubernetes" in suggest_skills(session, "kub")
    assert suggest_skills(session, "p") == []
    assert suggest_skills(session, "%%") == []
    assert len(suggest_skills(session, "co")) <= 10


async def test_the_suggestion_route_needs_a_login_and_answers_with_names(
    client: httpx.AsyncClient, loaded: dict[str, LabelEntry]
) -> None:
    assert (await client.get("/profile/skills/suggest", params={"q": "post"})).status_code == 303
    await _register(client)
    response = await client.get("/profile/skills/suggest", params={"q": "post"})
    assert response.status_code == 200
    assert "PostgreSQL" in response.json()


async def test_the_profile_page_offers_suggestions_and_credits_esco(client: httpx.AsyncClient) -> None:
    await _register(client)
    text = (await client.get("/profile")).text
    assert 'id="skill-suggestions"' in text
    assert "This service uses the ESCO classification of the European Commission." in text
    assert "adapted version" in text
