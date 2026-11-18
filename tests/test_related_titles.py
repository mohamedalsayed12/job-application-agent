"""Related job titles: asked of the AI once per title, kept for everyone, searched and matched."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.applicants.service import create_applicant, parse_preference_input
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.linkedin import searches
from app.matching.match import TitleMeaning, match_all, worth_judging
from app.matching.related_titles import cleaned, expand_titles, with_related
from app.matching.titles import NOT_ENTRY_TITLE
from app.models import ApplicantJobMatch, ApplicantProfile, BudgetPolicy, ExperienceLevel, RelatedTitle, SpendKind, SpendLedger
from tests.conftest import WordEmbedder
from tests.jobs import DAY_1, _job, _li, _public_resolve, _store

RELATED = [
    "Backend Developer",
    "Python Developer",
    "Java Developer",
    "Senior Python Developer",
    "Engineering Manager",
    "Software Engineering Intern",
    "Apprentice Developer",
    "Graduate Developer",
    "Full Stack Developer",
    "Frontend Developer",
    "Web Developer",
    "Application Developer",
    "Platform Engineer",
    "DevOps Engineer",
    "Mobile Developer",
]
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


class _Claude:
    def __init__(self, titles: list[str] | None = None, *, status: int = 200) -> None:
        self.titles, self.status, self.asked = titles if titles is not None else RELATED, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["messages"][0]["content"]
        self.asked.append(json.loads(re.search(r"\{.*\}", sent).group())["title"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"type": "overloaded_error"}})
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps({"titles": self.titles})}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 300, "output_tokens": 100},
            },
        )


def _settings() -> Settings:
    return Settings(ai_provider="anthropic", anthropic_api_key="test-key")  # type: ignore[call-arg]


async def _expand(session: Session, claude: _Claude, roles: list[str]) -> int:
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.anthropic.com"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(claude),
    )
    try:
        return await expand_titles(session, roles, gateway=gateway, settings=_settings(), persist=session.flush, now=NOW)
    finally:
        await gateway.aclose()


def _money(session: Session, cap: str = "5") -> None:
    session.add(
        BudgetPolicy(
            provider="*", period="month", money_cap=Decimal(cap), request_cap=None, currency="GBP",
            effective_from=NOW - timedelta(days=1), effective_to=None,
        )
    )
    session.flush()


def _applicant(session: Session, roles: list[str], locations: list[str], levels: list[ExperienceLevel]) -> uuid.UUID:
    preference = parse_preference_input(target_roles=roles, locations=locations)
    applicant = create_applicant(session, email=f"{uuid.uuid4()}@example.com", preference=preference)
    session.flush()
    session.add(ApplicantProfile(applicant_id=applicant.id, experience_levels=levels))
    session.flush()
    return applicant.id


def _spend(session: Session) -> list[SpendLedger]:
    return list(session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_related_titles)))


def test_level_internship_and_apprenticeship_titles_are_dropped() -> None:
    titles = cleaned({"titles": [" Python  Developer", "python developer", "Software Engineer", *RELATED]}, "software engineer")
    assert titles == [
        "Python Developer",
        "Backend Developer",
        "Java Developer",
        "Full Stack Developer",
        "Frontend Developer",
        "Web Developer",
        "Application Developer",
        "Platform Engineer",
        "DevOps Engineer",
    ]


async def test_a_title_is_asked_about_once_and_the_answer_is_kept_for_everyone(session: Session) -> None:
    _money(session)
    claude = _Claude()

    assert await _expand(session, claude, ["Graduate Software Engineer", "software engineer"]) == 1
    assert await _expand(session, claude, ["Junior Software Engineer"]) == 0

    assert claude.asked == ["software engineer"]
    [spend] = _spend(session)
    assert spend.actual_cost == Decimal("0.0010")
    assert with_related(session, ["Junior Software Engineer"])[:3] == ["Junior Software Engineer", "Backend Developer", "Python Developer"]


async def test_with_no_money_left_only_the_typed_titles_are_used(session: Session) -> None:
    _money(session, "0")
    claude = _Claude()

    assert await _expand(session, claude, ["Software Engineer"]) == 0

    assert claude.asked == []
    assert with_related(session, ["Software Engineer"]) == ["Software Engineer"]


async def test_a_failed_call_keeps_nothing_and_is_asked_again_next_time(session: Session) -> None:
    _money(session)

    assert await _expand(session, _Claude(status=500), ["Software Engineer"]) == 0
    assert session.scalar(select(RelatedTitle)) is None
    assert all(row.actual_cost in (None, Decimal(0)) for row in _spend(session))

    assert await _expand(session, _Claude(), ["Software Engineer"]) == 1


async def test_searches_are_capped_at_ten_titles_a_place_and_keep_the_persons_levels(session: Session) -> None:
    _money(session)
    roles = ["Graduate Software Engineer", "Junior Data Analyst"]
    _applicant(session, roles, ["Manchester", "Leeds"], [ExperienceLevel.entry])
    await _expand(session, _Claude(), roles)

    found = searches(session, None, per_person=10)

    related = cleaned({"titles": RELATED}, "software engineer")
    for place in ("Manchester", "Leeds"):
        titles = {search.role for search in found if search.location == place}
        assert titles == {*roles, *related[:8]}
        assert not any(NOT_ENTRY_TITLE.search(title) for title in titles)
    assert {parse_qs(urlsplit(search.url(posted_within_days=3)).query)["f_E"][0] for search in found} == {"2"}
    assert searches(session, None, per_person=None) == [search for search in found if search.role in roles]


def test_a_job_sharing_a_word_with_a_related_title_is_worth_judging() -> None:
    entry, meaning = frozenset({ExperienceLevel.entry}), TitleMeaning(WordEmbedder)
    assert not worth_judging("Junior QA Tester", ["Graduate Software Engineer"], entry, meaning)
    assert worth_judging("Junior QA Tester", ["Graduate Software Engineer", "QA Tester"], entry, meaning)


async def test_a_job_with_a_related_title_is_matched(session: Session) -> None:
    _money(session)
    roles = ["Graduate Software Engineer"]
    applicant = _applicant(session, roles, ["Manchester"], [ExperienceLevel.entry])
    _store(session, [_li(1, title="Junior Python Developer"), _li(2, title="Junior Account Executive")])
    assert match_all(session, now=DAY_1 + timedelta(hours=1)).matches == 0

    await _expand(session, _Claude(), roles)
    match_all(session, now=DAY_1 + timedelta(hours=1))

    [row] = session.scalars(select(ApplicantJobMatch).where(ApplicantJobMatch.applicant_id == applicant))
    assert row.job_id == _job(session, 1).id
    assert (row.reasons["role"]["target"], row.reasons["role"]["kind"]) == ("Python Developer", "exact")


@pytest.mark.parametrize("roles", [["Software Engineer"], []])
def test_with_related_keeps_the_typed_titles_first(session: Session, roles: list[str]) -> None:
    assert with_related(session, roles) == roles
