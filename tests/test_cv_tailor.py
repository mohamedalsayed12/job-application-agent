"""D55: Apply/Consider jobs get a grounded tailored CV with ATS coverage."""

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
from app.models import SpendKind, SpendLedger, TailoredCv, TailoredCvStatus
from app.tailor.ats import ats_report, coverage_for
from app.tailor.company import official_domain_from_apply_url
from app.tailor.draft import grounded_draft
from app.tailor.render import render_docx, render_pdf
from app.tailor.run import write_tailored_cvs
from tests.jobs import _public_resolve
from tests.test_cv_read import _docx
from tests.test_fit_report import CV_LINES, _Ai, _person_with_a_cv, _report
from tests.test_judge import _policy, _settings
from tests.test_matching import SOON

_DRAFT = {
    "contact": {
        "name": "Mohammed Example",
        "email": "",
        "phone": "",
        "location": "Manchester",
        "links": [],
    },
    "summary": "Graduate who built a REST API in Python with FastAPI and PostgreSQL.",
    "skills": ["Python", "FastAPI", "PostgreSQL"],
    "experience": [
        {
            "employer": "student society",
            "title": "",
            "start": "2024",
            "end": "Present",
            "bullets": ["Built a REST API in Python with FastAPI and PostgreSQL for a student society."],
        }
    ],
    "education": [
        {
            "school": "University of Manchester",
            "degree": "BSc Business Management",
            "start": "2024",
            "end": "2027",
            "details": "",
        }
    ],
}


class _TailorAi(_Ai):
    """Fit-report AI that also answers keyword and tailor prompts."""

    def __init__(self) -> None:
        super().__init__(judge_score=4.5, report=_report(score=4.5, verdict="Apply"))
        self.tailor_calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Company About / robots fetches hit this transport too; refuse them softly.
        if "api.anthropic.com" not in str(request.url):
            return httpx.Response(404, text="not found")
        try:
            body = json.loads(request.content)
        except json.JSONDecodeError:
            return httpx.Response(400, text="bad request")
        system = body.get("system", "")
        if "Career Ops" in system or "You evaluate one job" in system:
            return super().__call__(request)
        if "You extract hiring keywords" in system:
            self.tailor_calls.append("keywords")
            answer = {
                "keywords": [
                    {"term": "Python", "priority": "required", "rubric_bucket": "python"},
                    {"term": "Teamwork", "priority": "preferred", "rubric_bucket": "teamwork"},
                    {"term": "Kubernetes", "priority": "preferred", "rubric_bucket": "cloud_devops"},
                ]
            }
        elif "You revise one structured CV" in system:
            self.tailor_calls.append("revise")
            answer = _DRAFT
        elif "You tailor one CV" in system:
            self.tailor_calls.append("tailor")
            answer = _DRAFT
        else:
            return super().__call__(request)
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2000, "output_tokens": 800},
            },
        )


async def _prepare(session: Session, ai: _TailorAi):
    applicant = await _person_with_a_cv(session)
    gateway = ai.gateway()
    settings = _settings(ai_report_unit_price=Decimal("0.0015"), ai_tailor_unit_price=Decimal("0.003"))
    await judge_jobs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    await write_reports(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    match_all(session, now=SOON)
    return applicant, gateway, settings


def test_grounding_strips_invented_metrics() -> None:
    cv = "\n".join(CV_LINES)
    fake = {
        "contact": {"name": "Mohammed Example", "email": "", "phone": "", "location": "", "links": []},
        "summary": "Led a team of 40 engineers at Google.",
        "skills": ["Python", "Rust"],
        "experience": [
            {
                "employer": "Google",
                "title": "Staff Engineer",
                "start": "2010",
                "end": "2020",
                "bullets": ["Grew revenue by 900%."],
            }
        ],
        "education": [],
    }
    out = grounded_draft(fake, cv=cv, profile="Target roles: Backend Developer")
    assert out is not None
    assert out["skills"] == ["Python"]
    assert out["experience"] == []
    assert out["summary"] == ""


def test_ats_coverage_honesty() -> None:
    draft = grounded_draft(_DRAFT, cv="\n".join(CV_LINES), profile="")
    assert draft is not None
    pdf = render_pdf(draft)
    assert render_docx(draft)
    report = ats_report(
        pdf_bytes=pdf,
        draft=draft,
        keywords=[
            {"term": "Python", "priority": "required", "rubric_bucket": "python"},
            {"term": "Kubernetes", "priority": "preferred", "rubric_bucket": "cloud_devops"},
        ],
        master_cv="\n".join(CV_LINES),
    )
    by_term = {row["term"]: row["status"] for row in report["coverage"]}
    assert by_term["Python"] == "covered"
    assert by_term["Kubernetes"] == "missing (gap)"
    assert report["checks"]["text_extracted"]


def test_missing_have_it_when_master_cv_has_term() -> None:
    rows = coverage_for(
        [{"term": "FastAPI", "priority": "required", "rubric_bucket": "apps_apis_tools"}],
        extracted="Python and PostgreSQL",
        master_cv="\n".join(CV_LINES),
    )
    assert rows[0]["status"] == "missing (have it)"


def test_company_domain_skips_linkedin() -> None:
    assert official_domain_from_apply_url("https://www.linkedin.com/jobs/view/123") is None
    assert official_domain_from_apply_url("https://careers.acme.com/jobs/1") == "acme.com"


async def test_apply_job_gets_ready_package(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)

    run = await write_tailored_cvs(
        session, gateway=gateway, settings=settings, persist=session.flush, now=SOON
    )

    assert run.outcomes["ready"] == 1
    assert "keywords" in ai.tailor_calls and "tailor" in ai.tailor_calls
    row = session.scalars(select(TailoredCv)).one()
    assert row.status == TailoredCvStatus.ready.value
    assert row.pdf_content and row.docx_content
    assert row.ats_report and row.keywords
    spends = [
        ledger.kind
        for ledger in session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_cv_tailor))
    ]
    assert spends
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    html = render_listing(listings[0])
    assert "Tailored CV ready" in html
    assert f"/jobs/{listings[0].job_id}/tailored.pdf" in html
    assert row.company_research_available is False


async def test_hash_reuse_skips_second_run(session: Session) -> None:
    ai = _TailorAi()
    _, gateway, settings = await _prepare(session, ai)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    first_calls = list(ai.tailor_calls)

    run = await write_tailored_cvs(
        session, gateway=gateway, settings=settings, persist=session.flush, now=SOON
    )

    assert run.outcomes.get("ready", 0) == 0
    assert ai.tailor_calls == first_calls


async def test_budget_wait_leaves_waiting_status(session: Session) -> None:
    from tests.test_judge import _jobs
    from tests.test_matching import _applicant

    ai = _TailorAi()
    # Cap covers judge + report but not a tailor call (£0.003).
    _policy(session, Decimal("0.004"))
    await _jobs(session, ("Backend Developer", "Manchester, UK"))
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    store_cv(session, applicant, _docx(CV_LINES))
    session.flush()
    gateway = ai.gateway()
    settings = _settings(
        ai_judge_unit_price=Decimal("0.0005"),
        ai_report_unit_price=Decimal("0.0015"),
        ai_tailor_unit_price=Decimal("0.003"),
    )
    await judge_jobs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    await write_reports(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    run = await write_tailored_cvs(
        session, gateway=gateway, settings=settings, persist=session.flush, now=SOON
    )

    assert run.stopped_by == "budget"
    row = session.scalars(select(TailoredCv)).one()
    assert row.status == TailoredCvStatus.waiting_budget.value


async def test_download_is_owner_only(session: Session) -> None:
    from fastapi.testclient import TestClient

    from app.api.accounts import require_csrf
    from app.core.config import get_settings
    from app.core.db import get_db
    from app.main import app

    ai = _TailorAi()
    _, gateway, settings = await _prepare(session, ai)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    job_id = session.scalars(select(TailoredCv)).one().job_id

    def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_csrf] = lambda: None
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            anon = client.get(f"/jobs/{job_id}/tailored.pdf", follow_redirects=False)
            assert anon.status_code in {302, 303, 401, 403, 404}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
