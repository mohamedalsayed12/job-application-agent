"""Post-MVP apply agent: answers profile, mapper, prepare, extension API, cascade delete."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.applicants.apply_answers import AnswersInput, save_answers
from app.applications.extension_device import issue_token
from app.applications.mapper import (
    FillPlan,
    FillStep,
    FormField,
    Match,
    build_fill_plan,
    facts_from_profile,
    match_answer_key,
    matches_from_payload,
)
from app.applications.prepare import store_fill_outcome
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import (
    PREFER_NOT,
    Applicant,
    Application,
    ApplicationAnswers,
    ApplicationStatus,
    CvFile,
    CvFileType,
    ExtensionDevice,
    Job,
    TailoredCv,
    TailoredCvStatus,
)
from tests.jobs import _li, _store

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

    async def _no_model(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.applications.plan.model_matches", _no_model)
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
                **{"applyUrl": "https://careers.example.com/jobs/12345/apply"},
            )
        ],
    )
    return session.scalar(select(Job).where(Job.title == "Platform Engineer"))


def test_mapper_recognises_common_labels() -> None:
    assert match_answer_key("Do you require visa sponsorship?") == "needs_visa_sponsorship"
    assert match_answer_key("Have you had free school meals?") == "free_school_meals"
    assert match_answer_key("Upload your resume / CV") == "cv"
    assert match_answer_key("Right to work in the UK") == "right_to_work_uk"


def test_mapper_does_not_false_match_common_traps() -> None:
    assert match_answer_key("Tell us why you want this role") is None
    assert match_answer_key("Sexual orientation") is None
    assert match_answer_key("Hotel preferences") is None
    assert match_answer_key("Intelligence") is None
    assert match_answer_key("Gender") == "gender"


def _field(field_id: str, label: str, name: str, field_type: str, required: bool = False, options: list[str] | None = None) -> FormField:
    return FormField(id=field_id, label=label, name=name, type=field_type, required=required, options=options or [])


def test_fill_plan_maps_answers_and_flags_gaps() -> None:
    fields = [
        _field("name", "First name", "first_name", "text", True),
        _field("visa", "Visa sponsorship", "sponsorship", "select", True, ["Yes", "No"]),
        _field("fsm", "Free school meals", "fsm", "select", False, ["Yes", "No", "Prefer not to say"]),
        _field("cv", "Resume", "resume", "file", True),
        _field("why", "Secret custom question", "custom", "text", True),
    ]
    facts = facts_from_profile(
        {
            "legal_first_name": "Ada",
            "needs_visa_sponsorship": False,
            "free_school_meals": PREFER_NOT,
            "email": EMAIL,
        },
        [],
    )
    plan = build_fill_plan(fields, facts)
    actions = {step.field_id: step for step in plan.steps}
    assert actions["name"].value == "Ada"
    assert actions["visa"].value == "No"
    assert actions["fsm"].value == "Prefer not to say"
    assert actions["cv"].action == "upload_cv"
    assert [question.label for question in plan.missing] == ["Secret custom question"]

    decline = build_fill_plan(
        [_field("eth", "Ethnicity", "eth", "select", False, ["Yes", "No", "I do not want to answer"])],
        facts_from_profile({"ethnicity": PREFER_NOT}, []),
    )
    assert decline.steps[0].value == "I do not want to answer"


def test_an_invented_model_value_is_rejected() -> None:
    field = _field("why", "Why do you want this role?", "why", "textarea", True)
    facts = facts_from_profile({"email": EMAIL}, [])
    invented = matches_from_payload({"matches": [{"field_id": "why", "value": "I am a world-class ninja"}]})
    assert invented["why"] == Match()
    plan = build_fill_plan(
        [field],
        facts,
        cv_text="Ada Lovelace builds tools.",
        matches={"why": Match(cv_quote="I am a world-class ninja")},
    )
    assert plan.steps == []
    assert plan.missing[0].label == "Why do you want this role?"

    quoted = build_fill_plan(
        [field],
        facts,
        cv_text="Ada Lovelace builds tools.",
        matches={"why": Match(cv_quote="Ada Lovelace builds tools.")},
    )
    assert quoted.steps[0].value == "Ada Lovelace builds tools."
    assert quoted.steps[0].source == "cv"


def test_a_cv_quote_cannot_be_letters_inside_a_word() -> None:
    field = _field("visa", "Do you need visa sponsorship?", "visa", "select", True, ["Yes", "No"])
    plan = build_fill_plan(
        [field],
        facts_from_profile({"email": EMAIL}, []),
        cv_text="North London knowledge and another note",
        matches={"visa": Match(cv_quote="No")},
    )
    assert plan.steps == []
    assert plan.missing[0].label == "Do you need visa sponsorship?"


def test_a_complete_plan_reopens_a_failed_application() -> None:
    row = Application(
        applicant_id=uuid4(),
        job_id=uuid4(),
        apply_url="https://careers.example.com/jobs/1/apply",
        status=ApplicationStatus.failed.value,
        missing_fields=["Why"],
        fill_plan_json={"steps": []},
        error="nope",
    )
    store_fill_outcome(row, FillPlan(steps=[FillStep("f0", "fill", "Ada", "answers")], missing=[], attach=[]))
    assert row.status == ApplicationStatus.ready_to_fill.value
    assert row.missing_fields == []
    assert row.error is None
    assert row.fill_plan_json["steps"][0]["value"] == "Ada"


async def test_prepare_readies_the_job_for_the_extension(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    job = _job(session)

    response = await client.post("/applications", data={"job_id": str(job.id)})

    assert response.status_code == 303
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    assert response.headers["location"] == f"/applications/{row.id}"
    assert row.status == ApplicationStatus.ready_to_fill.value
    assert row.apply_url == "https://careers.example.com/jobs/12345/apply"


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
        fill_plan_json=None,
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


def _master_cv(session: Session, applicant: Applicant) -> None:
    session.add(
        CvFile(
            applicant_id=applicant.id,
            content=PDF,
            file_type=CvFileType.pdf.value,
            size_bytes=len(PDF),
            uploaded_at=datetime.now(UTC),
        )
    )


def _ready_application(session: Session, applicant: Applicant, job: Job) -> Application:
    row = Application(
        applicant_id=applicant.id,
        job_id=job.id,
        apply_url=job.apply_url,
        status=ApplicationStatus.ready_to_fill.value,
        fill_plan_json={"steps": [], "missing": []},
        missing_fields=[],
    )
    session.add(row)
    session.flush()
    return row


async def test_the_extension_uploads_the_tailored_cv_for_that_job(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    applicant = _applicant(session)
    job = _job(session)
    _master_cv(session, applicant)
    row = _ready_application(session, applicant, job)
    token = issue_token(session, applicant.id)
    tailored = b"%PDF-1.7\ntailored for Platform Engineer"
    session.add(
        TailoredCv(
            applicant_id=applicant.id,
            job_id=job.id,
            status=TailoredCvStatus.ready.value,
            input_hash="h",
            prompt_version="test",
            pdf_content=tailored,
            pdf_size_bytes=len(tailored),
        )
    )
    session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get(f"/extension/cv?application_id={row.id}", headers=headers)).content == tailored
    assert (await client.get("/extension/cv", headers=headers)).content == PDF


async def test_without_a_ready_tailored_cv_the_extension_uploads_the_uploaded_cv(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _register(client)
    applicant = _applicant(session)
    job = _job(session)
    _master_cv(session, applicant)
    row = _ready_application(session, applicant, job)
    token = issue_token(session, applicant.id)
    session.add(TailoredCv(applicant_id=applicant.id, job_id=job.id, status=TailoredCvStatus.pending.value, input_hash="h", prompt_version="test"))
    session.flush()

    response = await client.get(f"/extension/cv?application_id={row.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.content == PDF


async def test_after_review_the_person_marks_the_application_submitted(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    await _register(client)
    applicant = _applicant(session)
    job = _job(session)
    row = _ready_application(session, applicant, job)
    row.status = ApplicationStatus.filled_awaiting_review.value
    session.flush()

    assert "press Submit on the employer" in (await client.get(f"/jobs/{job.id}")).text
    assert (await client.post(f"/jobs/{job.id}/applied")).status_code == 303
    session.refresh(row)
    assert row.status == ApplicationStatus.submitted.value
    assert "You applied for this job" in (await client.get(f"/jobs/{job.id}")).text


async def test_a_job_applied_for_elsewhere_can_be_marked_applied(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    await _register(client)
    job = _job(session)
    assert "Prepare apply" in (await client.get(f"/jobs/{job.id}")).text

    assert (await client.post(f"/jobs/{job.id}/applied")).status_code == 303
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    assert row.status == ApplicationStatus.submitted.value
    assert row.applicant_id == _applicant(session).id

    again = await client.post("/applications", data={"job_id": str(job.id)})
    assert again.status_code == 303
    assert "apply_error" in again.headers["location"]
    session.refresh(row)
    assert row.status == ApplicationStatus.submitted.value


async def test_the_extension_cannot_mark_an_application_submitted(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    applicant = _applicant(session)
    row = _ready_application(session, applicant, _job(session))
    token = issue_token(session, applicant.id)
    session.flush()

    response = await client.post(
        f"/extension/applications/{row.id}/status",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "submitted"},
    )
    assert response.status_code == 400
    session.refresh(row)
    assert row.status == ApplicationStatus.ready_to_fill.value


def test_extension_source_never_submits() -> None:
    root = Path(__file__).resolve().parents[1] / "extension"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.js"))
    assert "requestSubmit" not in source
    assert ".submit(" not in source
    assert "KeyboardEvent" not in source
    assert "https://*/*" not in (root / "manifest.json").read_text(encoding="utf-8")


def _plan_fields(*rows: tuple[str, str, str]) -> dict:
    return {
        "fields": [
            {"id": field_id, "label": label, "name": name, "type": "textarea" if name == "why" else "text", "required": True, "options": []}
            for field_id, label, name in rows
        ]
    }


async def test_missing_answer_is_named_saved_and_reused(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _register(client)
    applicant = _applicant(session)
    save_answers(
        session,
        applicant.id,
        AnswersInput(
            legal_first_name="Ada",
            legal_last_name="Lovelace",
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
    prepared = await client.post("/applications", data={"job_id": str(job.id)})
    assert prepared.status_code == 303
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    token = issue_token(session, applicant.id)
    session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    asked = _plan_fields(("f0", "First name", "first_name"), ("f1", "Why do you want this role?", "why"))

    first = await client.post(f"/extension/applications/{row.id}/plan", headers=headers, json=asked)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == ApplicationStatus.needs_input.value
    assert body["plan"]["steps"] == []
    assert "Why do you want this role?" in body["notice"]
    page = await client.get("/applications")
    assert "Why do you want this role?" in page.text
    assert "Answer these questions, then fill again" in page.text

    saved = await client.post(
        f"/applications/{row.id}/answers",
        data={"question_0": "Why do you want this role?", "answer_0": "I like building tools."},
    )
    assert saved.status_code == 303
    session.refresh(row)
    assert row.status == ApplicationStatus.ready_to_fill.value
    assert row.fill_plan_json is None

    again = await client.post(f"/extension/applications/{row.id}/plan", headers=headers, json=asked)
    assert again.status_code == 200
    filled = {step["field_id"]: step for step in again.json()["plan"]["steps"]}
    assert filled["f0"]["value"] == "Ada"
    assert filled["f1"]["value"] == "I like building tools."
    assert "click Submit yourself" in again.json()["notice"]

    _store(
        session,
        [_li(9002, title="Support Engineer", **{"applyUrl": "https://careers.example.com/jobs/9002/apply"})],
    )
    other = session.scalar(select(Job).where(Job.title == "Support Engineer"))
    assert (await client.post("/applications", data={"job_id": str(other.id)})).status_code == 303
    second = session.scalar(select(Application).where(Application.job_id == other.id))
    reworded = _plan_fields(("f0", "Please tell us why you want this role", "why"))
    reused = await client.post(f"/extension/applications/{second.id}/plan", headers=headers, json=reworded)
    assert reused.status_code == 200
    assert reused.json()["plan"]["missing"] == []
    assert reused.json()["plan"]["steps"][0]["value"] == "I like building tools."


async def test_plan_rejects_an_invented_model_value(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _invented(*_args, **_kwargs):
        return {"f0": Match(cv_quote="I am a world-class ninja")}

    monkeypatch.setattr("app.applications.plan.model_matches", _invented)
    await _register(client)
    applicant = _applicant(session)
    job = _job(session)
    await client.post("/applications", data={"job_id": str(job.id)})
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    token = issue_token(session, applicant.id)
    session.flush()

    response = await client.post(
        f"/extension/applications/{row.id}/plan",
        headers={"Authorization": f"Bearer {token}"},
        json=_plan_fields(("f0", "Why do you want this role?", "why")),
    )
    assert response.status_code == 200
    assert response.json()["plan"]["steps"] == []
    assert response.json()["status"] == ApplicationStatus.needs_input.value
    assert "Why do you want this role?" in response.json()["notice"]
