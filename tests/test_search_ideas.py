"""D64: suggesting job titles and places to search from the person's own profile, before any search."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.profile import add_ideas
from app.applicants.search_ideas import ProfileAsk, RoleIdea, SearchIdeas, checked
from app.core.config import Settings
from app.models import ApplicantPreference, SpendKind, SpendLedger
from tests.test_cv_read import _Claude, _fields, _money, client  # noqa: F401
from tests.test_profile import PASSWORD

ASK = ProfileAsk(
    skills=("Python", "SQL", "Excel"),
    degrees=("BSc Business Management",),
    experience_levels=("entry",),
    target_roles=("Junior Python Developer",),
    locations=("Manchester",),
    travel_miles=None,
)
IDEAS = {
    "roles": [
        {"title": "Graduate Data Analyst", "because": ["sql", "Excel", "Kubernetes"]},
        {"title": "Junior  Business Analyst", "because": ["BSc Business Management"]},
        {"title": "Junior Rust Developer", "because": ["Rust"]},
        {"title": "Senior Data Analyst", "because": ["SQL"]},
        {"title": "Graduate Project Manager", "because": ["BSc Business Management"]},
        {"title": "Data Analyst Internship", "because": ["SQL"]},
        {"title": "junior python developer", "because": ["Python"]},
    ],
    "places": [
        {"name": "Salford", "near": "Manchester", "miles": 3},
        {"name": "manchester", "near": "Manchester", "miles": 0},
        {"name": "Stockport", "near": "manchester", "miles": 7},
        {"name": "Leeds", "near": "Manchester", "miles": 36},
        {"name": "Chester", "near": "Liverpool", "miles": 10},
        {"name": "Bolton", "near": "Manchester", "miles": "close"},
        {"name": "Eccles", "near": "Salford", "miles": 2},
    ],
}


def test_d64_roles_must_rest_on_the_profile_and_suit_an_entry_level_person() -> None:
    found = checked(IDEAS, ASK)
    assert found.roles == (
        RoleIdea("Graduate Data Analyst", ("SQL", "Excel")),
        RoleIdea("Junior Business Analyst", ("BSc Business Management",)),
    )
    assert found.places == ("Salford", "Stockport")


def test_d64_places_beyond_the_travel_distance_or_near_nowhere_listed_are_dropped() -> None:
    assert checked(IDEAS, _ask(travel_miles=40)).places == ("Salford", "Stockport", "Leeds")
    assert checked(IDEAS, _ask(travel_miles=5)).places == ("Salford",)


def test_d64_an_internship_seeker_gets_internships_but_not_senior_roles() -> None:
    found = checked(IDEAS, _ask(experience_levels=("internship",)))
    assert [role.title for role in found.roles] == [
        "Graduate Data Analyst",
        "Junior Business Analyst",
        "Data Analyst Internship",
    ]


def test_d64_someone_who_also_wants_mid_level_may_get_titles_above_entry() -> None:
    found = checked(IDEAS, _ask(experience_levels=("entry", "mid")))
    assert [role.title for role in found.roles] == [
        "Graduate Data Analyst",
        "Junior Business Analyst",
        "Senior Data Analyst",
        "Graduate Project Manager",
    ]


def test_d64_no_places_are_suggested_without_a_place_to_start_from() -> None:
    assert checked(IDEAS, _ask(locations=())).places == ()


def _ask(**changes: object) -> ProfileAsk:
    values = {name: getattr(ASK, name) for name in ProfileAsk.__slots__}
    return ProfileAsk(**{**values, **changes})  # type: ignore[arg-type]


@pytest.fixture
def claude() -> _Claude:
    return _Claude(IDEAS)


FORM = {
    "target_roles": "Junior Python Developer",
    "locations": "Manchester",
    "experience_levels": "entry",
    "skills": "Python\nSQL\nExcel",
    "degrees": "BSc Business Management",
}


async def _register(client: httpx.AsyncClient) -> None:  # noqa: F811
    assert (await client.post("/register", data={"email": "ideas@example.com", "password": PASSWORD})).status_code == 303


async def test_d64_suggestions_fill_the_form_and_save_nothing(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    _money(session, "2")
    await _register(client)
    response = await client.post("/profile/ideas", data=FORM)

    assert response.status_code == 200
    assert "Junior Python Developer\nGraduate Data Analyst\nJunior Business Analyst</textarea>" in response.text
    assert "Manchester\nSalford\nStockport</textarea>" in response.text
    assert "Graduate Data Analyst (from your SQL, Excel)" in response.text
    assert "your profile runs 9 LinkedIn searches a day, costing up to £0.63" in response.text
    assert "<PROFILE_DATA>" in claude.sent and "ideas@example.com" not in claude.sent
    assert session.scalar(select(ApplicantPreference)) is None
    spend = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_search_ideas)).all()
    assert len(spend) == 1


async def test_d64_without_skills_or_a_degree_the_ai_is_not_asked(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    _money(session, "2")
    await _register(client)
    response = await client.post("/profile/ideas", data={**FORM, "skills": "", "degrees": ""})
    assert "Add your skills or a degree first" in response.text
    assert claude.calls == 0


async def test_d64_with_no_money_left_the_profile_is_not_sent(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    _money(session, "0")
    await _register(client)
    response = await client.post("/profile/ideas", data=FORM)
    assert "AI budget is used up" in response.text
    assert claude.calls == 0


def test_d64_suggestions_stop_at_the_profile_limits() -> None:
    full = _fields(target_roles="\n".join(f"Role {n}" for n in range(10)), locations="Manchester")
    merged, notice = add_ideas(full, SearchIdeas((RoleIdea("Graduate Data Analyst", ("SQL",)),), ("Salford",)), Settings())
    assert merged.target_roles.count("\n") == 9
    assert merged.locations == "Manchester\nSalford"
    assert "20 LinkedIn searches a day" in notice


async def test_d64_unsaved_form_text_is_held_to_the_profile_limits(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    _money(session, "2")
    await _register(client)
    await client.post("/profile/ideas", data={**FORM, "skills": "\n".join(f"Skill{n}" for n in range(200))})
    assert "Skill49" in claude.sent and "Skill50" not in claude.sent
