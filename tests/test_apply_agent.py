"""Post-MVP apply agent: answers profile, mapper, prepare, extension API, cascade delete."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.applicants.apply_answers import AnswersInput, save_answers
from app.applications.extension_device import issue_token
from app.applications.fill_safety import assert_plan_has_no_submit_clicks, is_submit_control
from app.applications.mapper import build_fill_plan, match_answer_key
from app.applications.prepare import prepare_application
from app.applications.schema import FormField, FormSchema, parse_greenhouse_board
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    PREFER_NOT,
    Applicant,
    Application,
    ApplicationAnswers,
    ApplicationStatus,
    AtsKind,
    BudgetPolicy,
    CvFile,
    CvFileType,
    ExtensionDevice,
    Job,
    JobFreshness,
    SpendKind,
    SpendLedger,
)
from tests.jobs import _li, _public_resolve, _store

EMAIL = "apply-agent@example.com"
PASSWORD = "correct horse battery"
PDF = b"%PDF-1.7\ncv for apply agent"


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ADMIN_KEY", "test-admin-key-not-default")
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


async def _register(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": EMAIL, "password": PASSWORD})).status_code == 303


def _applicant(session: Session) -> Applicant:
    return session.scalar(select(Applicant).where(Applicant.email == EMAIL))


def _job(session: Session) -> Job:
    _store(
        session,
        [
            _li(
                9001,
                title="Platform Engineer",
                **{"applyUrl": "https://boards.greenhouse.io/acme/jobs/12345"},
            )
        ],
    )
    return session.scalar(select(Job).where(Job.title == "Platform Engineer"))


def test_mapper_recognises_common_labels() -> None:
    assert match_answer_key("Do you require visa sponsorship?") == "needs_visa_sponsorship"
    assert match_answer_key("Have you had free school meals?") == "free_school_meals"
    assert match_answer_key("Upload your resume / CV") == "cv"
    assert match_answer_key("Right to work in the UK") == "right_to_work_uk"


def test_fill_plan_maps_answers_and_flags_gaps() -> None:
    schema = FormSchema(
        ats_kind=AtsKind.greenhouse,
        source="stub",
        fields=[
            FormField("First name", "first_name", "text", True),
            FormField("Visa sponsorship", "sponsorship", "yes_no", True),
            FormField("Free school meals", "fsm", "select", False, ["Yes", "No", "Prefer not to say"]),
            FormField("Resume", "resume", "file", True),
            FormField("Secret custom question", "custom", "text", True),
        ],
    )
    answers = {
        "legal_first_name": "Mo",
        "needs_visa_sponsorship": False,
        "free_school_meals": PREFER_NOT,
        "cv": "upload_cv",
        "email": EMAIL,
    }
    plan = build_fill_plan(schema, answers)
    actions = {s.label: s for s in plan.steps}
    assert actions["First name"].value == "Mo"
    assert actions["Visa sponsorship"].value == "No"
    assert actions["Free school meals"].value == "Prefer not to say"
    assert actions["Resume"].action == "upload_cv"
    assert plan.missing == ["Secret custom question"]
    assert_plan_has_no_submit_clicks(plan.to_json())


def test_submit_controls_are_detected() -> None:
    assert is_submit_control(tag="button", type_attr="submit", text="Save")
    assert is_submit_control(tag="button", type_attr="button", text="Apply now")
    assert not is_submit_control(tag="button", type_attr="button", text="Next section")


def test_greenhouse_url_parse() -> None:
    assert parse_greenhouse_board("https://boards.greenhouse.io/acme/jobs/12345") == ("acme", "12345")
    assert parse_greenhouse_board(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=99"
    ) == ("acme", "99")


async def test_prepare_uses_greenhouse_questions(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(client)
    applicant = _applicant(session)
    save_answers(
        session,
        applicant.id,
        AnswersInput(
            legal_first_name="Mo",
            legal_last_name="Ali",
            phone="07000000000",
            address_line="1 High St",
            city="Manchester",
            postcode="M1 1AA",
            country="United Kingdom",
            linkedin_url="",
            github_url="",
            portfolio_url="",
            right_to_work_uk="yes",
            needs_visa_sponsorship="no",
            nationality="British",
            heard_about="Job board",
            previously_employed="no",
            free_school_meals=PREFER_NOT,
            gender=PREFER_NOT,
            ethnicity=PREFER_NOT,
            disability=PREFER_NOT,
            veteran=PREFER_NOT,
        ),
    )
    job = _job(session)
    session.add(
        CvFile(
            applicant_id=applicant.id,
            content=PDF,
            file_type=CvFileType.pdf.value,
            size_bytes=len(PDF),
            uploaded_at=datetime.now(UTC),
        )
    )
    session.flush()

    payload = {
        "id": 12345,
        "questions": [
            {
                "label": "Do you require visa sponsorship?",
                "name": "sponsorship",
                "type": "boolean",
                "required": True,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "boards-api.greenhouse.io" in request.url.host:
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    gateway = FetchGateway(
        user_agent="test",
        allowlist=frozenset({"boards-api.greenhouse.io"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),
    )

    async def _gw() -> AsyncIterator[FetchGateway]:
        yield gateway

    app.dependency_overrides[get_gateway] = _gw
    response = await client.post("/applications", data={"job_id": str(job.id)})
    assert response.status_code == 303
    assert "/applications/" in response.headers["location"]
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    assert row is not None
    assert row.status == ApplicationStatus.ready_to_fill.value
    assert row.ats_kind == AtsKind.greenhouse.value
    assert any(step["action"] == "upload_cv" for step in row.fill_plan_json["steps"])


async def test_extension_ready_cv_and_status(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    applicant = _applicant(session)
    job = _job(session)
    session.add(
        CvFile(
            applicant_id=applicant.id,
            content=PDF,
            file_type=CvFileType.pdf.value,
            size_bytes=len(PDF),
            uploaded_at=datetime.now(UTC),
        )
    )
    app_row = Application(
        applicant_id=applicant.id,
        job_id=job.id,
        apply_url=job.apply_url,
        status=ApplicationStatus.ready_to_fill.value,
        ats_kind=AtsKind.greenhouse.value,
        fill_plan_json={"steps": [{"label": "First name", "name": "first_name", "action": "fill", "value": "Mo", "required": True}], "missing": []},
        missing_fields=[],
    )
    session.add(app_row)
    token = issue_token(session, applicant.id)
    session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    ready = await client.get("/extension/applications/ready", headers=headers)
    assert ready.status_code == 200
    assert ready.json()[0]["id"] == str(app_row.id)
    cv = await client.get("/extension/cv", headers=headers)
    assert cv.status_code == 200
    assert cv.content == PDF
    status = await client.post(
        f"/extension/applications/{app_row.id}/status",
        headers=headers,
        json={"status": "filled_awaiting_review"},
    )
    assert status.status_code == 200
    session.refresh(app_row)
    assert app_row.status == ApplicationStatus.filled_awaiting_review.value


async def test_deleting_account_removes_apply_data(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    applicant = _applicant(session)
    applicant_id = applicant.id
    save_answers(
        session,
        applicant_id,
        AnswersInput(
            legal_first_name="Mo",
            legal_last_name="Ali",
            phone="",
            address_line="",
            city="",
            postcode="",
            country="",
            linkedin_url="",
            github_url="",
            portfolio_url="",
            right_to_work_uk="yes",
            needs_visa_sponsorship="no",
            nationality="",
            heard_about="",
            previously_employed="no",
            free_school_meals=PREFER_NOT,
            gender=PREFER_NOT,
            ethnicity=PREFER_NOT,
            disability=PREFER_NOT,
            veteran=PREFER_NOT,
        ),
    )
    job = _job(session)
    session.add(
        Application(
            applicant_id=applicant_id,
            job_id=job.id,
            apply_url=job.apply_url,
            status=ApplicationStatus.ready_to_fill.value,
            missing_fields=[],
        )
    )
    issue_token(session, applicant_id)
    session.flush()
    assert session.scalar(select(func.count()).select_from(ApplicationAnswers)) == 1
    assert session.scalar(select(func.count()).select_from(ExtensionDevice)) == 1

    response = await client.post("/account/delete", data={"password": PASSWORD})
    assert response.status_code == 200
    session.expire_all()
    assert session.get(Applicant, applicant_id) is None
    assert session.scalar(select(func.count()).select_from(ApplicationAnswers)) == 0
    assert session.scalar(select(func.count()).select_from(Application)) == 0
    assert session.scalar(select(func.count()).select_from(ExtensionDevice)) == 0


async def test_apify_form_extract_respects_budget(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.applications.extract import extract_form_schema

    monkeypatch.setenv("APIFY_API_TOKEN", "token")
    monkeypatch.setenv("APPLY_FORM_ACTOR", "owner/form-actor")
    get_settings.cache_clear()
    settings = get_settings()
    # Cap at zero so reserve fails.
    session.add(
        BudgetPolicy(
            provider="apify",
            period="month",
            money_cap=Decimal("0"),
            request_cap=None,
            currency="GBP",
            effective_from=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    session.flush()
    gateway = FetchGateway(
        user_agent="test",
        allowlist=frozenset({"api.apify.com"}),
        resolve=_public_resolve,
    )
    result = await extract_form_schema(
        session,
        apply_url="https://jobs.lever.co/acme/abc",
        gateway=gateway,
        settings=settings,
        persist=session.flush,
    )
    assert result.budget_blocked is True
    get_settings.cache_clear()


async def test_apply_answers_page_saves(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    response = await client.post(
        "/profile/apply-answers",
        data={
            "legal_first_name": "Mo",
            "legal_last_name": "Ali",
            "right_to_work_uk": "yes",
            "needs_visa_sponsorship": "no",
            "previously_employed": "no",
            "free_school_meals": PREFER_NOT,
            "gender": PREFER_NOT,
            "ethnicity": PREFER_NOT,
            "disability": PREFER_NOT,
            "veteran": PREFER_NOT,
        },
    )
    assert response.status_code == 303
    row = session.scalar(select(ApplicationAnswers))
    assert row is not None
    assert row.legal_first_name == "Mo"
    assert row.needs_visa_sponsorship is False
    assert row.free_school_meals == PREFER_NOT
