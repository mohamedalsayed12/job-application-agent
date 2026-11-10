"""D50: the AI judges whether each open job fits each person, and matching follows its verdict."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.listings import load_listings, render_listing
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.matching.judge import judge_jobs
from app.matching.match import match_all
from app.models import ApplicantSkill, BudgetPolicy, JobVerdict, SpendLedger
from tests.test_ai_extraction import _public_resolve
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import SOON, _applicant, _gh, _matches

ADVERT = "Build payment services in Python and PostgreSQL for our platform team. " * 4


class _Judge:
    """Answers like the Messages API with a verdict per job title, and records what it was sent."""

    def __init__(self, verdicts: dict[str, tuple[bool, str, str]]) -> None:
        self.verdicts, self.sent = verdicts, []
        self.score: object = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][0]["content"]
        self.sent.append(content)
        fits, level, reason = next(v for title, v in self.verdicts.items() if f"Title: {title}\\n" in content)
        answer = {
            "level": level,
            "cv_match": 4 if fits else 2,
            "role_alignment": 5 if fits else 2,
            "comp": 3,
            "culture": 4,
            "red_flags": [],
            "score": self.score if self.score is not None else (4.3 if fits else 2.4),
            "reason": reason,
        }
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 900, "output_tokens": 40},
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
            effective_from=SOON - timedelta(days=1),
            effective_to=None,
        )
    )
    session.flush()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(ai_provider="anthropic", anthropic_api_key="test-key", ai_judge_unit_price=Decimal("0.0003"))
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _judge(session: Session, judge: _Judge, **settings: object):
    return await judge_jobs(session, gateway=judge.gateway(), settings=_settings(**settings), persist=session.flush, now=SOON)


async def _jobs(session: Session, *titles_and_cities: tuple[str, str]) -> None:
    _board(session)
    jobs = [_gh(n, title=title, city=city, content=ADVERT) for n, (title, city) in enumerate(titles_and_cities, start=1)]
    await _Collector(session).read(200, {"jobs": jobs})


async def test_d50_matches_follow_the_ai_verdict_over_the_rules(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Payments Developer", "Manchester, UK"), ("Backend Developer", "Manchester, UK"))
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)
    assert set(_matches(session, applicant)) == {_job(session, 2).id}

    judge = _Judge(
        {
            "Payments Developer": (True, "entry", "It builds payment services in Python, which matches your skills."),
            "Backend Developer": (False, "senior", "It needs senior experience."),
        }
    )
    report = await _judge(session, judge)
    assert (report.stopped_by, report.outcomes) == (None, {"fits": 1, "no_fit": 1})
    match_all(session, now=SOON)

    matches = _matches(session, applicant)
    # The rules missed the first job and matched the second; the AI's verdict decides both.
    assert set(matches) == {_job(session, 1).id}
    ai_match = matches[_job(session, 1).id]
    assert ai_match.reasons["role"]["kind"] == "ai"
    assert ai_match.reasons["role"]["level"] == "entry level, judged by AI"

    listing = load_listings(session, applicant, everywhere=False, page_number=1)[0][0]
    page = render_listing(listing)
    assert "AI match" in page
    assert "AI&#x27;s reason: It builds payment services in Python, which matches your skills." in page
    assert "AI score: 4.3 out of 5 (added at 4.0 or more)" in page
    assert "AI ratings: your profile 4/5, your target roles 5/5, pay 3/5, culture 4/5" in page

    spent = session.scalars(select(SpendLedger).where(SpendLedger.kind == "ai_job_judge")).all()
    assert len(spent) == 2


async def test_d50_a_verdict_is_reused_until_the_profile_changes(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Backend Developer": (True, "mid", "It is a backend role.")})

    await _judge(session, judge)
    await _judge(session, judge)
    assert len(judge.sent) == 1
    assert "Target roles: Backend Developer" in judge.sent[0]
    assert "Manchester" not in judge.sent[0].split("<JOB_POSTING_DATA>")[0]

    session.add(ApplicantSkill(applicant_id=applicant, text_as_typed="Python"))
    session.flush()
    await _judge(session, judge)
    assert len(judge.sent) == 2
    assert "Skills: Python" in judge.sent[1]


async def test_d50_jobs_in_the_persons_locations_are_judged_first_and_the_cap_stops_the_rest(session: Session) -> None:
    _policy(session, Decimal("0.0003"))
    await _jobs(session, ("Backend Developer", "Berlin, Germany"), ("Python Developer", "Manchester, UK"))
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Backend Developer": (True, "mid", "Fits."), "Python Developer": (True, "mid", "Fits.")})

    report = await _judge(session, judge)

    assert report.stopped_by == "budget"
    assert [row.job_id for row in session.scalars(select(JobVerdict))] == [_job(session, 2).id]


async def test_d50_a_reason_naming_something_in_neither_text_is_not_shown(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Backend Developer": (True, "mid", "You would work with Kubernetes at Google.")})

    await _judge(session, judge)

    verdict = session.scalars(select(JobVerdict)).one()
    assert verdict.fits is True
    assert verdict.reason is None


async def test_d50_without_an_anthropic_key_nothing_is_judged(session: Session) -> None:
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({})

    report = await _judge(session, judge, anthropic_api_key="")

    assert report.stopped_by == "not_configured"
    assert judge.sent == []


async def test_d50_titles_far_from_the_roles_cost_nothing(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Domestic Surveyor", "Manchester, UK"), ("Python Developer", "Manchester, UK"))
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Python Developer": (True, "mid", "It is a Python role.")})

    report = await _judge(session, judge)

    assert report.outcomes == {"fits": 1}
    assert len(judge.sent) == 1


async def test_d51_only_a_score_of_4_or_more_is_added(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Backend Developer": (True, "mid", "It is a backend role.")})

    judge.score = 3.9
    report = await _judge(session, judge)
    match_all(session, now=SOON)

    assert report.outcomes == {"no_fit": 1}
    verdict = session.scalars(select(JobVerdict)).one()
    assert (verdict.fits, verdict.score) == (False, Decimal("3.9"))
    assert verdict.ratings == {"cv_match": 4, "role_alignment": 5, "comp": 3, "culture": 4, "red_flags": []}
    assert _matches(session, applicant) == {}


async def test_d51_a_score_outside_1_to_5_is_not_used(session: Session) -> None:
    _policy(session, Decimal("1"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    judge = _Judge({"Backend Developer": (True, "mid", "It is a backend role.")})

    judge.score = 7
    report = await _judge(session, judge)

    assert report.outcomes == {"unreadable": 1}
    assert session.scalars(select(JobVerdict)).all() == []
