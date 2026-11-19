"""D60: tailored CVs come from Resume-Matcher's path-diff pipeline, held to the master CV."""

from __future__ import annotations

import copy
import io
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete, select


from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate
from sqlalchemy.orm import Session

from app.api.listings import load_listings, render_listing
from app.applicants.cv import store_cv
from app.matching.judge import judge_jobs
from app.matching.match import match_all
from app.matching.report import write_reports
from app.models import (
    Applicant,
    ApplicantJobMatch,
    ApplicantPreference,
    ApplicantProfile,
    BudgetPolicy,
    CvFile,
    ExperienceLevel,
    Job,
    JobReport,
    SpendKind,
    SpendLedger,
    TailoredCv,
    TailoredCvStatus,
)
from app.tailor.ats import docx_text, pdf_text


from app.tailor.ats import ats_report
from app.tailor.improver import Change, apply_diffs
from app.tailor.preservation import finalize_ai_resume
from app.tailor.refiner import remove_ai_phrases
from app.tailor.render import (
    render_docx,
    render_letter_docx,
    render_letter_pdf,
    render_pdf,
)
from app.tailor.resume import ground_parsed
from app.tailor.run import request_tailored_cv, write_tailored_cvs
from tests.test_cv_read import _docx
from tests.test_fit_report import CV_LINES, _Ai, _person_with_a_cv, _report
from tests.test_judge import _policy, _settings
from tests.test_matching import SOON

CV = "\n".join(CV_LINES)
BULLET = "Built a REST API in Python with FastAPI and PostgreSQL for a student society."

_PARSED = {
    "personalInfo": {
        "name": "Mohammed Example",
        "title": "",
        "email": "mo@invented.example",
        "phone": "",
        "location": "",
        "website": "",
        "linkedin": "",
        "github": "",
    },
    "summary": "",
    "workExperience": [
        {
            "id": 1,
            "title": "",
            "company": "student society",
            "location": "",
            "years": "",
            "description": [BULLET],
        }
    ],
    "education": [
        {
            "id": 1,
            "institution": "University of Manchester",
            "degree": "BSc Business Management",
            "years": "2027",
            "description": "",
        }
    ],
    "personalProjects": [],
    "additional": {
        "technicalSkills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes"],
        "languages": [],
        "certificationsTraining": [],
        "awards": [],
    },
}
MASTER = ground_parsed(_PARSED, CV)

_KEYWORDS = {
    "company": "Acme",
    "role": "Backend Developer",
    "required_skills": ["Python", "REST API"],
    "preferred_skills": ["Kubernetes"],
    "experience_requirements": [],
    "education_requirements": [],
    "key_responsibilities": [],
    "keywords": ["teamwork"],
    "experience_years": None,
    "seniority_level": "entry",
}

_DIFFS = {
    "changes": [
        {
            "path": "workExperience[0].description[0]",
            "action": "replace",
            "original": BULLET,
            "value": "Spearheaded a Python web service with FastAPI and PostgreSQL for a student society.",
            "reason": "posting wording",
        },
        {
            "path": "workExperience[0].company",
            "action": "replace",
            "original": "student society",
            "value": "Google",
            "reason": "bigger name",
        },
        {
            "path": "additional.technicalSkills",
            "action": "reorder",
            "original": None,
            "value": ["PostgreSQL", "Kubernetes", "Python", "FastAPI"],
            "reason": "relevance",
        },
    ],
    "strategy_notes": "",
}


def _injected() -> dict:
    out = copy.deepcopy(MASTER)
    out["workExperience"][0]["description"] = [
        "Spearheaded a REST API web service in Python with FastAPI and PostgreSQL for a student society."
    ]
    out["additional"]["technicalSkills"] = ["PostgreSQL", "Python", "FastAPI", "Docker"]
    return out


LETTER = (
    "Dear Hiring Manager,\n\n"
    "Acme's REST API work is why this role stands out to me. I spearheaded a REST API in Python "
    "with FastAPI and PostgreSQL for a student society — the same stack you name.\n\n"
    "Kind regards,\nMohammed Example"
)


class _TailorAi(_Ai):
    """Fit-report AI that also answers the five tailoring prompts."""

    def __init__(self, letter: str = LETTER) -> None:
        super().__init__(judge_score=4.5, report=_report(score=4.5, verdict="Apply"))
        self.tailor_calls: list[str] = []
        self.letter = letter

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "api.anthropic.com" not in str(request.url):
            return httpx.Response(404, text="not found")
        system = json.loads(request.content).get("system", "")
        answers = {
            "You are a JSON extraction engine": ("parse", lambda: _PARSED),
            "You are an expert job description analyzer": ("keywords", lambda: _KEYWORDS),
            "You are an expert resume editor": ("diffs", lambda: _DIFFS),
            "You are a resume editor. Inject keywords": ("inject", _injected),
            "You are a professional career coach": ("letter", lambda: {"letter": self.letter}),
        }
        for prefix, (name, answer) in answers.items():
            if system.startswith(prefix):
                self.tailor_calls.append(name)
                return httpx.Response(
                    200,
                    json={
                        "model": "claude-haiku-5-5",
                        "content": [{"type": "text", "text": json.dumps(answer())}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 2000, "output_tokens": 800},
                    },
                )
        return super().__call__(request)


class _FailParse(_TailorAi):
    def __init__(self) -> None:
        super().__init__()
        self.fail_parse = False
        self.parse_failures = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content).get("system", "") if request.content else ""
        if self.fail_parse and system.startswith("You are a JSON extraction engine"):
            self.fail_parse = False
            self.parse_failures += 1
            return httpx.Response(500, json={"error": "down"})
        return super().__call__(request)


async def _prepare(session: Session, ai: _TailorAi):
    applicant = await _person_with_a_cv(session)
    gateway = ai.gateway()
    settings = _settings(ai_report_unit_price=Decimal("0.0015"), ai_tailor_unit_price=Decimal("0.003"))
    await judge_jobs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    await write_reports(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    match_all(session, now=SOON)
    return applicant, gateway, settings


def _package(session: Session, applicant_id: uuid.UUID) -> TailoredCv:
    return session.scalars(select(TailoredCv).where(TailoredCv.applicant_id == applicant_id)).one()


def test_parsed_cv_keeps_only_what_the_cv_shows() -> None:
    parsed = copy.deepcopy(_PARSED)
    parsed["workExperience"].append(
        {"id": 2, "title": "Staff Engineer", "company": "Google", "location": "", "years": "2010 - 2020",
         "description": ["Grew revenue by 900%."]}
    )

    master = ground_parsed(parsed, CV)

    assert [entry["company"] for entry in master["workExperience"]] == ["student society"]
    assert master["additional"]["technicalSkills"] == ["Python", "FastAPI", "PostgreSQL"]
    assert master["personalInfo"]["email"] == ""
    assert master["personalInfo"]["name"] == "Mohammed Example"


def test_only_quoted_edits_on_allowed_paths_land() -> None:
    changes = [
        Change("workExperience[0].description[0]", "replace", BULLET, "Built a Python REST API.", ""),
        Change("workExperience[0].company", "replace", "student society", "Google", ""),
        Change("workExperience[0].description[0]", "replace", "text the CV never had", "Anything.", ""),
        Change("additional.technicalSkills", "reorder", None, ["PostgreSQL", "Rust", "Python"], ""),
    ]

    tailored, applied, rejected = apply_diffs(MASTER, changes)

    assert tailored["workExperience"][0]["description"] == ["Built a Python REST API."]
    assert tailored["workExperience"][0]["company"] == "student society"
    assert tailored["additional"]["technicalSkills"] == ["PostgreSQL", "Python", "FastAPI"]
    assert len(applied) == 2 and len(rejected) == 2
    assert MASTER["workExperience"][0]["description"] == [BULLET]


def test_finalize_holds_the_cv_to_its_source() -> None:
    candidate = copy.deepcopy(MASTER)
    candidate["workExperience"][0]["company"] = "Google"
    candidate["workExperience"][0]["description"] = [BULLET.replace("society.", "society, cutting costs by 40%.")]
    candidate["education"] = []
    candidate["additional"]["technicalSkills"] = ["Python"]

    final = finalize_ai_resume(MASTER, candidate, allow_review_claims=False)

    assert final["workExperience"][0]["company"] == "student society"
    assert final["workExperience"][0]["description"] == [BULLET]
    assert final["education"] == MASTER["education"]
    assert final["additional"]["technicalSkills"] == ["Python", "FastAPI", "PostgreSQL"]


def test_finalize_reverts_a_rewrite_that_drifts_from_every_source_row() -> None:
    candidate = copy.deepcopy(MASTER)
    candidate["workExperience"][0]["description"] = ["Managed a sales team across three regions."]

    assert finalize_ai_resume(MASTER, candidate, allow_review_claims=False)["workExperience"][0]["description"] == [
        BULLET
    ]
    assert finalize_ai_resume(MASTER, candidate, allow_review_claims=True)["workExperience"][0]["description"] == [
        "Managed a sales team across three regions."
    ]


def test_stock_ai_wording_goes_unless_the_candidate_wrote_it() -> None:
    data = {"summary": "Spearheaded a robust API with stakeholders."}

    cleaned, removed = remove_ai_phrases(data, "", protected_text="")
    kept, _ = remove_ai_phrases(data, "", protected_text="I built a robust test suite.")

    assert cleaned["summary"] == "Led a strong API with team members."
    assert removed == ["robust", "spearheaded", "stakeholder"]
    assert kept["summary"] == "Led a robust API with team members."


def test_rendered_cv_has_an_ats_readable_text_layer() -> None:
    pdf = render_pdf(MASTER)

    text = pdf_text(pdf)
    docx = docx_text(render_docx(MASTER))
    assert "Mohammed Example" in text and "Mohammed Example" in docx
    assert "University of Manchester" in text and "University of Manchester" in docx
    assert "PostgreSQL" in text and "PostgreSQL" in docx
    assert "Experience" in text and "Experience" in docx
    assert render_docx(MASTER).startswith(b"PK")


def test_rendered_cv_text_is_the_resume_and_nothing_else() -> None:
    lines = pdf_text(render_pdf(MASTER)).splitlines()

    assert lines == [
        "Mohammed Example",
        "Skills",
        "Python, FastAPI, PostgreSQL",
        "Experience",
        "student society",
        "•",
        BULLET,
        "Education",
        "BSc Business Management · University of Manchester · 2027",
    ]


def _pdf_with_reportlab_bullet_names() -> bytes:
    buffer = io.BytesIO()
    style = getSampleStyleSheet()["Normal"]
    story = [
        Paragraph("Mohammed Example", style),
        ListFlowable([ListItem(Paragraph(BULLET, style), value="bullet")], bulletType="bullet", start="bullet"),
    ]
    SimpleDocTemplate(buffer).build(story)
    return buffer.getvalue()


def test_ats_check_fails_a_pdf_with_text_the_resume_lacks() -> None:
    def report(pdf: bytes) -> dict:
        return ats_report(pdf_bytes=pdf, docx_bytes=render_docx(MASTER), resume=MASTER, master=MASTER, keywords=_KEYWORDS, warnings=[])

    broken = report(_pdf_with_reportlab_bullet_names())
    assert broken["checks"]["no_stray_text"] is False
    assert broken["passed"] is False
    assert report(render_pdf(MASTER))["passed"] is True


async def test_apply_job_gets_ready_package(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)

    stored = session.scalar(select(CvFile).where(CvFile.applicant_id == applicant))
    stored.label = "Backend Developer"
    store_cv(session, applicant, b"%PDF-1.7\nother cv", label="Data Analyst")
    session.flush()

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert ai.tailor_calls == ["parse", "keywords", "diffs", "inject", "letter"]
    row = _package(session, applicant)
    assert row.status == TailoredCvStatus.ready.value
    assert row.cv_file_id == stored.id
    text = " ".join(pdf_text(row.pdf_content).split())
    assert "Led a REST API web service in Python with FastAPI and PostgreSQL for a student society." in text
    assert "Spearheaded" not in text
    assert "Google" not in text and "Docker" not in text and "Kubernetes" not in text
    assert "mo@invented.example" not in text
    assert row.draft["additional"]["technicalSkills"][0] == "PostgreSQL"
    assert row.docx_content
    status = {item["term"]: item["status"] for item in row.ats_report["coverage"]}
    assert status == {
        "Python": "covered",
        "REST API": "covered",
        "Kubernetes": "missing (gap)",
        "teamwork": "missing (gap)",
    }
    assert row.ats_report["keyword_match"] == 50
    assert any("Docker" in warning for warning in row.ats_report["warnings"])
    assert row.cost_amount == Decimal("0.003") * 5
    spends = session.scalars(
        select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_cv_tailor, SpendLedger.related_item_id == row.job_id)
    ).all()
    assert len(spends) == 5
    assert row.cover_letter.startswith("Dear Hiring Manager,")
    assert "I led a REST API in Python" in row.cover_letter
    assert "—" not in row.cover_letter
    assert any("cover letter" in warning for warning in row.ats_report["warnings"])
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    html = render_listing(listings[0])
    assert "Tailored CV ready" in html
    assert "Keyword match 50%" in html
    assert f"/jobs/{listings[0].job_id}/tailored.pdf" in html
    assert "tailored.docx" in html
    assert "from the advert" in html
    assert "Company website:" in html
    docx = docx_text(row.docx_content)
    assert "REST API" in docx
    assert row.ats_report["checks"]["one_column"]
    assert row.ats_report["checks"]["no_tables"]
    assert row.ats_report["checks"]["no_images"]
    assert row.ats_report["checks"]["docx_text_extracted"]
    assert row.ats_report["checks"]["standard_headings"]
    assert row.ats_report["passed"]
    assert f"/jobs/{listings[0].job_id}/cover-letter.pdf" in html


async def test_a_blank_letter_still_ships_the_cv(session: Session) -> None:
    ai = _TailorAi(letter="  ")
    applicant, gateway, settings = await _prepare(session, ai)

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    row = _package(session, applicant)
    assert row.status == TailoredCvStatus.ready.value
    assert row.pdf_content
    assert row.cover_letter is None
    assert "Cover letter skipped: empty" in row.ats_report["warnings"]
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    assert "cover-letter.pdf" not in render_listing(listings[0])


def test_rendered_letter_keeps_paragraphs_and_sign_off() -> None:
    text = " ".join(pdf_text(render_letter_pdf(LETTER, MASTER)).split())

    assert text.startswith("Mohammed Example")
    assert "Dear Hiring Manager," in text
    assert "Kind regards, Mohammed Example" in text
    assert render_letter_docx(LETTER, MASTER).startswith(b"PK")


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


async def test_a_cv_with_nothing_groundable_fails_the_package(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    monkeypatch.setitem(_PARSED, "workExperience", [])
    monkeypatch.setitem(_PARSED, "education", [])
    monkeypatch.setitem(_PARSED, "additional", {**_PARSED["additional"], "technicalSkills": ["Rust"]})

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    row = _package(session, applicant)
    assert row.status == TailoredCvStatus.failed.value
    assert row.draft == {"reason": "cv_not_parsed"}
    assert ai.tailor_calls == ["parse"]
    spent = session.scalars(
        select(SpendLedger.actual_cost).where(
            SpendLedger.kind == SpendKind.ai_cv_tailor, SpendLedger.related_item_id == row.job_id
        )
    ).all()
    assert spent == [Decimal("0.003")]


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
    match_all(session, now=SOON)

    run = await write_tailored_cvs(
        session, gateway=gateway, settings=settings, persist=session.flush, now=SOON
    )

    assert run.stopped_by == "budget"
    row = session.scalars(select(TailoredCv)).one()
    assert row.status == TailoredCvStatus.waiting_budget.value

    session.scalars(select(BudgetPolicy)).one().money_cap = Decimal(1)
    for _ in range(2):
        await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert session.scalars(select(TailoredCv)).one().status == TailoredCvStatus.ready.value
    assert ai.tailor_calls.count("parse") == 1


class _ScoredAi(_TailorAi):
    """Writes each job's fit report with the score set for its title."""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__()
        self.scores = scores

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "api.anthropic.com" not in str(request.url):
            return super().__call__(request)
        content = json.loads(request.content)["messages"][0]["content"]
        title = next((title for title in self.scores if json.dumps(f"Title: {title}\n")[1:-1] in content), None)
        if title is not None:
            self.report = _report(score=self.scores[title], verdict="Research first")
        return super().__call__(request)


async def _listed_jobs(session: Session, ai: _TailorAi, *titles: str):
    from tests.jobs import _store
    from tests.test_matching import ADVERT, _applicant, _posting

    _policy(session, Decimal(1))
    _store(session, [_posting(n, title=title, content=ADVERT) for n, title in enumerate(titles, start=1)])
    applicant = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    store_cv(session, applicant, _docx(CV_LINES))
    session.flush()
    gateway = ai.gateway()
    settings = _settings(ai_report_unit_price=Decimal("0.0015"), ai_tailor_unit_price=Decimal("0.003"))
    await judge_jobs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    await write_reports(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    match_all(session, now=SOON)
    return applicant, gateway, settings


def _tailored_titles(session: Session) -> dict[str, str]:
    return dict(
        session.execute(select(Job.title, TailoredCv.status).join(TailoredCv, TailoredCv.job_id == Job.id)).all()
    )


async def test_a_listed_job_scoring_3_or_more_is_tailored_whatever_its_verdict(session: Session) -> None:
    ai = _ScoredAi({"Backend Developer": 3.0})
    applicant, gateway, settings = await _listed_jobs(session, ai, "Backend Developer")
    listings, _ = load_listings(session, applicant, everywhere=False, page_number=1)
    assert [item.title for item in listings] == ["Backend Developer"]
    assert listings[0].reasons["report"]["verdict"] == "Research first"
    assert "Tailored CV queued." in render_listing(listings[0])

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert _tailored_titles(session) == {"Backend Developer": TailoredCvStatus.ready.value}
    listings, _ = load_listings(session, applicant, everywhere=False, page_number=1)
    assert "Tailored CV ready" in render_listing(listings[0])


async def test_jobs_hidden_from_listings_or_scored_under_3_are_never_tailored(session: Session) -> None:
    ai = _ScoredAi({"Data Engineer": 2.5, "Platform Developer Apprenticeship": 4.5})
    applicant, gateway, settings = await _listed_jobs(session, ai, "Data Engineer", "Platform Developer Apprenticeship")
    assert load_listings(session, applicant, everywhere=True, page_number=1) == ([], 0)

    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert run.outcomes == {}
    assert _tailored_titles(session) == {}
    assert ai.tailor_calls == []


async def test_the_best_scoring_listed_job_is_tailored_first(session: Session) -> None:
    ai = _ScoredAi({"Backend Developer": 3.2, "Python Developer": 4.6, "API Developer": 3.9})
    await _listed_jobs(session, ai, "Backend Developer", "Python Developer", "API Developer")

    gateway, settings = ai.gateway(), _settings(ai_tailor_unit_price=Decimal("0.003"), ai_tailor_per_cycle=1)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert _tailored_titles(session) == {"Python Developer": TailoredCvStatus.ready.value}

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert set(_tailored_titles(session)) == {"Python Developer", "API Developer"}


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    from app.accounts import throttle
    from app.api.forms import require_csrf
    from app.core.config import get_settings
    from app.core.db import get_db
    from app.main import app

    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ADMIN_KEY", "test-admin-key-not-default")
    # Job pages would otherwise write a summary with a real key.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("AI_APPLICANT_IDS", "")
    get_settings.cache_clear()
    throttle.reset()

    def _db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _person_on_a_job_without_a_report(session: Session, client: httpx.AsyncClient) -> tuple[Applicant, Job]:
    from tests.test_judge import _jobs

    _policy(session, Decimal(1))
    email = "tailor-button@example.com"
    assert (await client.post("/register", data={"email": email, "password": "correct horse battery"})).status_code == 303
    applicant = session.scalar(select(Applicant).where(Applicant.email == email))
    store_cv(session, applicant.id, _docx(CV_LINES))
    await _jobs(session, ("Data Analyst", "Leeds, UK"))
    job = session.scalar(select(Job).where(Job.title == "Data Analyst"))
    assert session.scalar(select(JobReport).where(JobReport.job_id == job.id)) is None
    return applicant, job


async def test_any_job_can_be_tailored_on_request(client: httpx.AsyncClient, session: Session) -> None:
    _, job = await _person_on_a_job_without_a_report(session, client)
    assert "Tailor my CV for this job" in (await client.get(f"/jobs/{job.id}")).text

    assert (await client.post(f"/jobs/{job.id}/tailor")).status_code == 303
    assert "Tailored CV queued." in (await client.get(f"/jobs/{job.id}")).text

    ai = _TailorAi()
    settings = _settings(ai_tailor_unit_price=Decimal("0.003"))
    run = await write_tailored_cvs(session, gateway=ai.gateway(), settings=settings, persist=session.flush, now=SOON)

    assert run.outcomes["ready"] == 1
    page = (await client.get(f"/jobs/{job.id}")).text
    assert "Tailored CV ready" in page
    assert "Tailor my CV for this job" not in page
    pdf = await client.get(f"/jobs/{job.id}/tailored.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert "Read the cover letter" in page
    letter = await client.get(f"/jobs/{job.id}/cover-letter.pdf")
    assert letter.status_code == 200
    assert "Dear Hiring Manager," in pdf_text(letter.content)
    assert (await client.get(f"/jobs/{job.id}/cover-letter.docx")).content.startswith(b"PK")


async def test_a_failed_package_can_be_tried_again(client: httpx.AsyncClient, session: Session) -> None:
    applicant, job = await _person_on_a_job_without_a_report(session, client)
    await client.post(f"/jobs/{job.id}/tailor")
    row = session.scalars(select(TailoredCv)).one()
    row.status = TailoredCvStatus.failed.value
    session.flush()
    assert "Try again" in (await client.get(f"/jobs/{job.id}")).text

    assert (await client.post(f"/jobs/{job.id}/tailor")).status_code == 303

    session.refresh(row)
    assert row.status == TailoredCvStatus.pending.value
    assert row.applicant_id == applicant.id


async def test_an_apply_job_that_is_not_listed_offers_the_button(
    client: httpx.AsyncClient, session: Session
) -> None:
    applicant, job = await _person_on_a_job_without_a_report(session, client)
    session.execute(delete(ApplicantJobMatch))
    session.add(
        JobReport(
            applicant_id=applicant.id,
            job_id=job.id,
            verdict="Apply",
            score=Decimal("4.0"),
            report={"verdict": "Apply"},
            input_hash="h",
            model="m",
            prompt_version="p",
        )
    )
    # Left behind by an earlier cycle when the job was still listed.
    session.add(
        TailoredCv(
            applicant_id=applicant.id,
            job_id=job.id,
            status=TailoredCvStatus.waiting_budget.value,
            input_hash="old",
            prompt_version="p",
        )
    )
    session.flush()

    page = (await client.get(f"/jobs/{job.id}")).text
    assert "Tailor my CV for this job" in page
    assert "waiting on the monthly budget" not in page

    await client.post(f"/jobs/{job.id}/tailor")
    assert "Tailor my CV for this job" not in (await client.get(f"/jobs/{job.id}")).text


async def test_people_outside_the_ai_list_get_no_button(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    _, job = await _person_on_a_job_without_a_report(session, client)
    monkeypatch.setenv("AI_APPLICANT_IDS", str(uuid.uuid4()))
    get_settings.cache_clear()

    assert "Tailor my CV for this job" not in (await client.get(f"/jobs/{job.id}")).text
    assert (await client.post(f"/jobs/{job.id}/tailor")).status_code == 403
    assert session.scalars(select(TailoredCv)).first() is None


async def test_download_is_owner_only(session: Session) -> None:
    from fastapi.testclient import TestClient

    from app.api.accounts import require_csrf
    from app.core.config import get_settings
    from app.core.db import get_db
    from app.main import app

    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    job_id = _package(session, applicant).job_id

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


_MACHINE = "Built a dynamic, results-driven REST API in Python with FastAPI and PostgreSQL for a student society."
_PAGE = (
    "<html><head><title>Acme</title></head><body>"
    + " ".join(["Payments platform reliability"] * 30)
    + "</body></html>"
).encode()


def _model(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 20},
        },
    )


class _Running(_TailorAi):
    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.seen: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "api.anthropic.com" in str(request.url):
            system = json.loads(request.content).get("system", "")
            if system.startswith("You are a JSON extraction engine"):
                row = self.session.scalar(select(TailoredCv))
                self.seen.append(row.status if row is not None else None)
        return super().__call__(request)


class _Machine(_TailorAi):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "api.anthropic.com" not in str(request.url):
            return super().__call__(request)
        system = json.loads(request.content).get("system", "")
        if system.startswith("You are a resume editor repairing"):
            self.tailor_calls.append("revise")
            return _model({"changes": [], "strategy_notes": ""})
        if system.startswith("You are an expert resume editor"):
            self.tailor_calls.append("diffs")
            return _model(
                {
                    "changes": [
                        {
                            "path": "workExperience[0].description[0]",
                            "action": "replace",
                            "original": BULLET,
                            "value": _MACHINE,
                            "reason": "tone",
                        }
                    ],
                    "strategy_notes": "",
                }
            )
        return super().__call__(request)


class _Site(_TailorAi):
    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host.endswith("acme.example"):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_PAGE)
        return super().__call__(request)


async def test_only_listed_jobs_scoring_3_or_more_are_tailored(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    report = session.scalars(select(JobReport)).one()
    job = session.get(Job, report.job_id)

    session.execute(delete(ApplicantJobMatch))
    session.flush()
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert session.scalars(select(TailoredCv)).first() is None

    match_all(session, now=SOON)
    job.freshness = "closed"
    session.flush()
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert session.scalars(select(TailoredCv)).first() is None

    job.freshness = "open"
    report.score = Decimal("2.5")
    report.report = {**report.report, "score": 2.5}
    session.flush()
    match_all(session, now=SOON)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert session.scalars(select(TailoredCv)).first() is None
    assert load_listings(session, applicant, everywhere=True, page_number=1) == ([], 0)

    request_tailored_cv(session, applicant, job.id, now=SOON)
    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert run.outcomes["ready"] == 1


async def test_a_listed_consider_job_is_queued_then_tailored(session: Session) -> None:
    ai = _Running(session)
    applicant, gateway, settings = await _prepare(session, ai)
    report = session.scalars(select(JobReport)).one()
    report.verdict = "Consider"
    report.report = {**report.report, "verdict": "Consider"}
    session.flush()
    match_all(session, now=SOON)
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    assert "Tailored CV queued." in render_listing(listings[0])
    assert "Tailor my CV" not in render_listing(listings[0])

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert "running" in ai.seen
    row = _package(session, applicant)
    assert row.status == TailoredCvStatus.ready.value
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    page = render_listing(listings[0])
    assert "Tailored CV ready" in page
    assert "Tailor my CV" not in page


async def test_a_machine_written_line_is_sent_back_and_then_restored(session: Session) -> None:
    ai = _Machine()
    applicant, gateway, settings = await _prepare(session, ai)

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert "revise" in ai.tailor_calls
    text = " ".join(pdf_text(_package(session, applicant).pdf_content).split())
    assert "dynamic" not in text.lower()
    assert "results-driven" not in text.lower()
    assert "Built a REST API in Python with FastAPI and PostgreSQL" in text


async def test_company_wording_is_shown_and_not_added_as_a_skill(session: Session) -> None:
    ai = _Site()
    applicant, gateway, settings = await _prepare(session, ai)

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    row = _package(session, applicant)
    sources = {item["term"]: item["source"] for item in row.keywords["sources"]}
    assert sources["Payments"] == "company"
    assert "Payments" not in row.draft["additional"]["technicalSkills"]
    text = " ".join(pdf_text(row.pdf_content).split())
    assert "Payments" not in text
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    card = render_listing(listings[0])
    assert "Payments" in card
    assert "from the company website" in card
    assert row.company_research_available is True


async def test_a_failed_refresh_keeps_the_file_and_does_not_retry(session: Session) -> None:
    ai = _FailParse()
    applicant, gateway, settings = await _prepare(session, ai)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    row = _package(session, applicant)
    previous = row.pdf_content
    old_hash = row.input_hash
    job = session.get(Job, row.job_id)
    assert job is not None
    job.description = (job.description or "") + "\nMust have owned a payments platform."
    ai.fail_parse = True

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert row.status == TailoredCvStatus.ready.value
    assert row.pdf_content == previous
    assert row.input_hash != old_hash
    assert row.draft["last_error"]
    failed_once = ai.parse_failures

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert ai.parse_failures == failed_once
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    card = render_listing(listings[0])
    assert "The latest attempt failed" in card
    assert "Try again" in card
    request_tailored_cv(session, applicant, row.job_id, now=SOON)
    assert row.status == TailoredCvStatus.pending.value


async def test_a_budget_wait_keeps_a_ready_cv_visible(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    row = _package(session, applicant)
    previous = row.pdf_content
    old_hash = row.input_hash
    job = session.get(Job, row.job_id)
    assert job is not None
    job.description = (job.description or "") + "\nAnother requirement."
    policy = session.scalars(select(BudgetPolicy)).one()
    policy.money_cap = Decimal("0")

    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert run.stopped_by == "budget"
    assert row.status == TailoredCvStatus.ready.value
    assert row.pdf_content == previous
    assert row.input_hash == old_hash
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    card = render_listing(listings[0])
    assert "tailored.pdf" in card
    assert "waiting on the monthly budget" not in card


def _away_from_the_job(session: Session, applicant_id: uuid.UUID) -> None:
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id))
    preference.locations = ["Leeds"]
    session.flush()
    match_all(session, now=SOON)
    assert session.scalar(select(ApplicantJobMatch.location_fit)) is False


async def test_an_apply_job_outside_my_locations_waits_for_a_click_by_default(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _away_from_the_job(session, applicant)
    job_id = session.scalars(select(JobReport.job_id)).one()

    await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert session.scalars(select(TailoredCv)).first() is None
    assert load_listings(session, applicant, everywhere=False, page_number=1)[0] == []
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    card = render_listing(listings[0], "<input>")
    assert "Tailor my CV for this job" in card
    assert "Tailored CV queued" not in card

    request_tailored_cv(session, applicant, job_id, now=SOON)
    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert run.outcomes["ready"] == 1


async def test_an_apply_job_outside_my_locations_is_tailored_when_i_widen_the_scope(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    _away_from_the_job(session, applicant)
    session.add(ApplicantProfile(applicant_id=applicant, experience_levels=["entry"], tailor_outside_locations=True))
    session.flush()

    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    assert "Tailored CV queued." in render_listing(listings[0])
    assert "Tailor my CV" not in render_listing(listings[0])
    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)

    assert run.outcomes["ready"] == 1
    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    assert "Tailored CV ready" in render_listing(listings[0])


async def test_a_package_left_unfinished_outside_the_scope_offers_the_button(session: Session) -> None:
    ai = _TailorAi()
    applicant, gateway, settings = await _prepare(session, ai)
    job_id = session.scalars(select(JobReport.job_id)).one()
    # Queued while the job was in the person's locations, then they moved.
    session.add(
        TailoredCv(
            applicant_id=applicant,
            job_id=job_id,
            status=TailoredCvStatus.pending.value,
            input_hash="old",
            prompt_version="p",
        )
    )
    _away_from_the_job(session, applicant)

    listings, _ = load_listings(session, applicant, everywhere=True, page_number=1)
    card = render_listing(listings[0], "<input>")
    assert "Tailor my CV for this job" in card
    assert "Tailored CV queued" not in card
    run = await write_tailored_cvs(session, gateway=gateway, settings=settings, persist=session.flush, now=SOON)
    assert run.outcomes.get("ready", 0) == 0


async def test_the_job_page_follows_the_tailoring_scope(client: httpx.AsyncClient, session: Session) -> None:
    applicant, job = await _person_on_a_job_without_a_report(session, client)
    session.add(
        JobReport(
            applicant_id=applicant.id,
            job_id=job.id,
            verdict="Apply",
            score=Decimal("4.0"),
            report={"verdict": "Apply"},
            input_hash="h",
            model="m",
            prompt_version="p",
        )
    )
    session.add(
        ApplicantJobMatch(
            applicant_id=applicant.id,
            job_id=job.id,
            score=Decimal("0.8"),
            reasons={"report": {"verdict": "Apply", "score": 4.0}},
            role_match=True,
            location_fit=False,
            salary_status="unknown",
        )
    )
    session.flush()

    page = (await client.get(f"/jobs/{job.id}")).text
    assert "Tailor my CV for this job" in page
    assert "Tailored CV queued" not in page

    session.add(ApplicantProfile(applicant_id=applicant.id, experience_levels=["entry"], tailor_outside_locations=True))
    session.flush()
    page = (await client.get(f"/jobs/{job.id}")).text
    assert "Tailored CV queued." in page
    assert "Tailor my CV for this job" not in page


def test_wording_that_claims_a_posting_term_the_cv_lacks_goes_back() -> None:
    from app.tailor.refiner import revert_unsupported_claims

    master = {
        "summary": "Python developer with automated testing experience.",
        "workExperience": [{"id": 1, "description": ["Wrote tests for customer matching."]}],
    }
    tailored = {
        "summary": "Python developer with JavaScript-adjacent, automated testing experience.",
        "workExperience": [{"id": 1, "description": ["Wrote tests for customer matching to monitor data accuracy."]}],
    }
    keywords = {"required_skills": ["JavaScript"], "preferred_skills": [], "keywords": ["monitoring", "automation"]}
    fixed, reverted = revert_unsupported_claims(tailored, master, keywords)
    assert fixed == master | {"workExperience": [{"id": 1, "description": ["Wrote tests for customer matching."]}]}
    assert sorted(reverted) == ["JavaScript", "monitoring"]


def test_phrase_requirements_are_met_by_their_skill_words() -> None:
    from app.tailor.resume import term_in_text

    cv = "Python, SQL, PostgreSQL. Resolved data issues; communication gaps. JavaScript-free."
    assert term_in_text("Knowledge of SQL databases", cv)
    assert term_in_text("Good communication skills", cv)
    assert not term_in_text("Knowledge of Javascript/Typescript", "I know JavaScript.")
