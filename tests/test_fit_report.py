"""D52: jobs the judge scores 3.0 or more get the full fit report, and its verdict shows on the page."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.listings import load_listings, render_listing
from app.applicants.cv import store_cv
from app.fetch.gateway import FetchGateway
from app.matching.judge import judge_jobs
from app.matching.match import match_all
from app.matching.report import write_reports
from app.models import JobReport, SpendLedger
from tests.test_ai_extraction import _public_resolve
from tests.test_cv_read import _docx
from tests.test_judge import _jobs, _policy, _settings
from tests.test_matching import SOON, _applicant, _job, _matches

CV_LINES = [
    "Mohammed Example",
    "Built a REST API in Python with FastAPI and PostgreSQL for a student society.",
    "BSc Business Management, University of Manchester, expected 2027.",
]


def _requirement(**overrides: object) -> dict:
    row = {
        "requirement": "Python",
        "importance": "critical",
        "basis": "explicit wording",
        "match": "Strong",
        "cv_quote": "Built a REST API in Python with FastAPI and PostgreSQL",
        "evidence": "",
    }
    row.update(overrides)
    return row


def _report(**overrides: object) -> dict:
    answer = {
        "requirements": [
            _requirement(),
            _requirement(requirement="Kubernetes", importance="preferred", match="Missing", cv_quote="", evidence="Not in the CV."),
            _requirement(requirement="Team player", importance="critical", basis="inference", match="Partial", cv_quote="", evidence="Society work."),
            _requirement(requirement="Go", importance="high", match="Strong", cv_quote="Ten years of Go at Google", evidence=""),
        ],
        "criteria": {
            key: {"assessment": "Fine.", "evidence": "Supported"}
            for key in ("cv_match", "career_direction", "compensation", "culture", "red_flags")
        },
        "advertised_pay": "not stated",
        "market_estimate": "none given",
        "hard_blockers": [],
        "assumptions": [],
        "score": 3.8,
        "verdict": "Apply",
        "verdict_reason": "It builds payment services in Python.",
        "confidence": "Medium",
        "questions": ["Is the role open to graduates?"],
    }
    answer.update(overrides)
    return answer


class _Ai:
    """Answers the judge with a fixed score and the report with a fixed report; records what each was sent."""

    def __init__(self, *, judge_score: float = 4.2, report: dict | None = None) -> None:
        self.judge_score, self.report = judge_score, report or _report()
        self.judged: list[str] = []
        self.reported: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = body["messages"][0]["content"]
        if "Career Ops" in body["system"]:
            self.reported.append(content)
            answer = self.report
        else:
            self.judged.append(content)
            answer = {
                "level": "entry",
                "cv_match": 4,
                "role_alignment": 5,
                "comp": 3,
                "culture": 3,
                "red_flags": [],
                "score": self.judge_score,
                "reason": "It is a backend role.",
            }
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3000, "output_tokens": 900},
            },
        )

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.anthropic.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )


async def _run(session: Session, ai: _Ai):
    gateway, settings = ai.gateway(), _settings(ai_report_unit_price=Decimal("0.0015"))
    await judge_jobs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    run = await write_reports(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    match_all(session, now=SOON)
    return run


async def _person_with_a_cv(session: Session):
    _policy(session, Decimal("1"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    store_cv(session, applicant, _docx(CV_LINES))
    session.flush()
    return applicant


async def test_d52_a_job_scoring_3_or_more_gets_a_checked_report_shown_on_the_page(session: Session) -> None:
    applicant = await _person_with_a_cv(session)
    ai = _Ai()

    run = await _run(session, ai)

    assert run.outcomes == {"Consider": 1}
    assert "Built a REST API in Python" in ai.reported[0]
    assert "Locations wanted: Manchester" in ai.reported[0]
    report = session.scalars(select(JobReport)).one().report
    # Apply needs 4.0 or more.
    assert (report["verdict"], report["score"]) == ("Consider", "3.8")
    rows = {row["requirement"]: row for row in report["requirements"]}
    # An inferred requirement is never above meaningful.
    assert rows["Team player"]["importance"] == "meaningful"
    # A Strong match needs a line that really is in the CV or profile.
    assert rows["Python"]["match"] == "Strong"
    assert (rows["Go"]["match"], rows["Go"]["cv_quote"]) == ("Partial", "")
    # Unmet requirements come first, most important first.
    assert [row["requirement"] for row in report["requirements"]] == ["Go", "Team player", "Kubernetes", "Python"]

    listing = load_listings(session, applicant, everywhere=False, page_number=1)[0][0]
    page = render_listing(listing)
    assert "Consider · 3.8/5" in page
    assert "<summary>Fit report</summary>" in page
    assert "Evidence confidence: Medium." in page
    assert "Is the role open to graduates?" in page
    spent = session.scalars(select(SpendLedger).where(SpendLedger.kind == "ai_fit_report")).all()
    assert len(spent) == 1


async def test_d52_a_report_scoring_below_3_takes_the_job_off_the_page(session: Session) -> None:
    applicant = await _person_with_a_cv(session)

    await _run(session, _Ai(report=_report(score=2.6, verdict="Skip")))

    assert _matches(session, applicant) == {}


async def test_d52_jobs_the_judge_scored_below_3_get_no_report(session: Session) -> None:
    await _person_with_a_cv(session)
    ai = _Ai(judge_score=2.5)

    run = await _run(session, ai)

    assert ai.reported == []
    assert run.written == set()


async def test_d52_a_report_is_reused_until_the_cv_changes(session: Session) -> None:
    applicant = await _person_with_a_cv(session)
    ai = _Ai()
    await _run(session, ai)
    await _run(session, ai)
    assert len(ai.reported) == 1

    store_cv(session, applicant, _docx([*CV_LINES, "Built a Django app for a local charity."]))
    session.flush()
    await _run(session, ai)

    assert len(ai.reported) == 2
    assert "Django" in ai.reported[1]
    assert _job(session, 1).id in _matches(session, applicant)
