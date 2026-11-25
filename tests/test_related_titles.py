"""D72: related job titles offered as tick-boxes, and the note that new titles are being searched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch.due import search_key
from app.linkedin import SOURCE, Search
from app.matching.related_titles import cleaned, related_to, title_key
from app.models import FetchOutcome, FetchRun, RelatedTitle, SpendKind, SpendLedger
from tests.auth import sign_up
from tests.test_cv_read import _Claude, _money, claude, client  # noqa: F401  (fixtures)

PASSWORD = "correct horse battery staple"
TITLES = {"titles": ["Backend Developer", "Senior Python Developer", "Python Developer", "Software Engineer", "Web Intern"]}


def test_a_title_is_asked_about_without_its_level_words() -> None:
    assert title_key("  Graduate   Software Engineer ") == "software engineer"
    assert title_key("Junior Entry-Level Python Developer") == "python developer"


def test_level_and_training_titles_and_the_title_itself_are_dropped() -> None:
    assert cleaned(TITLES, "Software Engineer") == ["Backend Developer", "Python Developer"]


async def test_the_picker_gets_related_titles_and_each_title_is_paid_for_once(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    claude.payload = TITLES
    _money(session, "5")
    assert (await sign_up(client, "titles@example.com", PASSWORD)).status_code == 303

    first = await client.post("/profile/titles/related", data={"title": ["Graduate Software Engineer"]})

    assert first.status_code == 200
    assert first.json() == ["Backend Developer", "Python Developer"]
    # Only the title goes to the AI, never the profile.
    assert '"title": "software engineer"' in claude.sent
    assert session.scalars(select(RelatedTitle.title_key)).all() == ["software engineer"]

    again = await client.post("/profile/titles/related", data={"title": ["Junior Software Engineer", "Backend Developer"]})

    # "software engineer" is already stored; "backend developer" is new. A title already picked is not offered.
    assert claude.calls == 2
    assert again.json() == ["Python Developer"]
    spent = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_related_titles)).all()
    assert len(spent) == 2


async def test_with_no_money_left_nothing_is_asked_and_nothing_breaks(
    client: httpx.AsyncClient, session: Session, claude: _Claude  # noqa: F811
) -> None:
    assert (await sign_up(client, "broke@example.com", PASSWORD)).status_code == 303

    response = await client.post("/profile/titles/related", data={"title": ["Data Analyst"]})

    assert (response.status_code, response.json(), claude.calls) == (200, [], 0)


async def test_the_picker_needs_a_login(client: httpx.AsyncClient) -> None:  # noqa: F811
    assert (await client.post("/profile/titles/related", data={"title": ["Data Analyst"]})).status_code == 303


def test_related_titles_come_back_in_the_order_of_the_roles(session: Session) -> None:
    session.add_all(
        [
            RelatedTitle(title_key="data analyst", titles=["BI Analyst"], model="m", prompt_version="related-titles-v1"),
            RelatedTitle(title_key="software engineer", titles=["Backend Developer", "Data Analyst"], model="m", prompt_version="related-titles-v1"),
        ]
    )
    session.flush()

    assert related_to(session, ["Software Engineer", "Junior Data Analyst"]) == ["Backend Developer", "BI Analyst"]


async def test_the_matches_page_says_when_new_titles_are_still_being_searched(
    client: httpx.AsyncClient, session: Session  # noqa: F811
) -> None:
    assert (await sign_up(client, "wait@example.com", PASSWORD)).status_code == 303
    profile = {"target_roles": "Data Analyst\nBI Analyst", "locations": "Leeds", "experience_level": "entry"}
    assert (await client.post("/profile", data=profile)).status_code == 303

    assert "We are running 2 searches for your job titles and locations now" in (await client.get("/listings")).text

    now = datetime.now(UTC)
    for role in ("Data Analyst", "BI Analyst"):
        session.add(
            FetchRun(
                source=SOURCE,
                outcome=FetchOutcome.empty.value,
                jobs_returned=0,
                request_count=1,
                started_at=now - timedelta(minutes=2),
                finished_at=now,
                search_key=search_key(Search(role, "Leeds")),
            )
        )
    session.flush()

    assert "We are searching for" not in (await client.get("/listings")).text
