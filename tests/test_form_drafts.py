"""D74: questions the saved answers don't cover: answered once and reused, or drafted from the CV and checked."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.applications.drafts import (
    PERSON_ONLY,
    STORY_QUESTION,
    _plausible,
    question_key,
)
from app.applications.extension_device import issue_token
from app.applications.schema import FormField
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    Application,
    ApplicationStatus,
    CvFile,
    CvFileType,
    FormQuestion,
    SpendKind,
    SpendLedger,
)
from tests.jobs import _public_resolve
from tests.test_apply_agent import SCANNED, _answers, _applicant, _job, _register
from tests.test_cv_read import _docx, _money

CV = [
    "Mo Ali",
    "Manchester | 07000000000",
    "BSc Business Management, University of Manchester, 2027",
    "Built a job search API in Python and FastAPI for 40 students at the careers society",
    "Barista, Costa Coffee: trained four new starters",
]
WHY = "I want to build Acme's platform tools. At university I built a job search API in Python and FastAPI."
DISABILITY = {"label": "Do you consider yourself to have a disability?", "name": "dis", "type": "select", "required": True, "options": ["Yes", "No"]}
FORM = dict(SCANNED, fields=[*SCANNED["fields"], DISABILITY])


class _Claude:
    """Answers each call with the next payload: the drafts, then the check."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads, self.sent = list(payloads), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content)["messages"][0]["content"])
        payload = self.payloads.pop(0)
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5000, "output_tokens": 600},
            },
        )


@pytest.fixture
def claude() -> _Claude:
    return _Claude(
        {"answers": [{"id": 0, "answer": WHY, "basis": "cv_and_advert"}, {"id": 1, "answer": "", "basis": "none"}]},
        {"checks": [{"id": 0, "ok": True, "fixed": "", "problem": ""}]},
    )


@pytest.fixture
async def client(session: Session, claude: _Claude, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    async def _gateway() -> AsyncIterator[FetchGateway]:
        gateway = FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.anthropic.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(claude),
        )
        yield gateway
        await gateway.aclose()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[get_gateway] = _gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _ready(client: httpx.AsyncClient, session: Session, *, cv: bool = True) -> tuple[Application, dict[str, str]]:
    await _register(client)
    applicant = _applicant(session)
    _answers(session, applicant)
    if cv:
        content = _docx(CV)
        session.add(
            CvFile(
                applicant_id=applicant.id, content=content, file_type=CvFileType.docx.value,
                size_bytes=len(content), uploaded_at=datetime.now(UTC),
            )
        )
    job = _job(session)
    await client.post("/applications", data={"job_id": str(job.id)})
    row = session.scalar(select(Application).where(Application.job_id == job.id))
    headers = {"Authorization": f"Bearer {issue_token(session, applicant.id)}"}
    session.flush()
    return row, headers


def _steps(plan: dict) -> dict[str, dict]:
    return {step["name"]: step for step in plan["steps"]}


# Drafting and checking.


async def test_open_questions_are_drafted_checked_and_marked(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    _money(session, "2")
    row, headers = await _ready(client, session)

    plan = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=FORM)).json()

    why = _steps(plan)["jaa-7"]
    assert (why["action"], why["value"], why["source"]) == ("fill", WHY, "ai")
    assert "checked" in why["note"]
    # Saved answers are still filled as before and are not marked.
    assert _steps(plan)["first_name"]["source"] == "saved"
    # A disability question needs the person: never sent to the AI, never filled, left for them.
    assert "dis" not in _steps(plan)
    assert plan["missing"] == [DISABILITY["label"]]
    assert all("disability" not in sent for sent in claude.sent)
    # Two calls: drafts, then the check of every draft against the CV and advert.
    assert len(claude.sent) == 2 and WHY in claude.sent[1]
    assert "Built a job search API" in claude.sent[0]
    spent = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_apply_map)).all()
    assert len(spent) == 2
    session.refresh(row)
    assert row.missing_fields == [DISABILITY["label"]]
    page = (await client.get(f"/applications/{row.id}")).text
    assert "Answers the AI drafted and checked for this form (1)" in page and "Acme&#x27;s platform tools" in page


async def test_a_draft_the_check_rejects_is_never_filled(session: Session, client: httpx.AsyncClient, claude: _Claude) -> None:
    claude.payloads[1] = {"checks": [{"id": 0, "ok": False, "fixed": "", "problem": "Claims a platform team role"}]}
    _money(session, "2")
    row, headers = await _ready(client, session)

    plan = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=SCANNED)).json()

    assert "jaa-7" not in _steps(plan)
    assert plan["missing"] == ["Why do you want to work at Acme?"]


async def test_a_draft_the_check_fixes_is_filled_as_fixed(session: Session, client: httpx.AsyncClient, claude: _Claude) -> None:
    claude.payloads[1] = {"checks": [{"id": 0, "ok": False, "fixed": "I built a job search API in Python.", "problem": "Too much"}]}
    _money(session, "2")
    row, headers = await _ready(client, session)

    plan = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=SCANNED)).json()

    assert _steps(plan)["jaa-7"]["value"] == "I built a job search API in Python."
    # Fixed wording, never the checker's own text, which can ramble or stop mid-word.
    note = _steps(plan)["jaa-7"]["note"]
    assert "corrected" in note and "Too much" not in note


async def test_without_money_or_a_cv_nothing_is_drafted(session: Session, client: httpx.AsyncClient, claude: _Claude) -> None:
    row, headers = await _ready(client, session, cv=False)
    _money(session, "2")
    plan = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=SCANNED)).json()
    assert plan["missing"] == ["Why do you want to work at Acme?"] and claude.sent == []


# The question bank.


async def test_an_open_question_is_answered_once_and_reused(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    row, headers = await _ready(client, session, cv=False)

    await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=FORM)

    banked = session.scalars(select(FormQuestion).order_by(FormQuestion.label)).all()
    assert [(q.label, q.answer) for q in banked] == [(DISABILITY["label"], None), ("Why do you want to work at Acme?", None)]
    page = (await client.get("/applications")).text
    assert f'action="/form-questions/{banked[1].id}"' in page

    # An option the form doesn't offer is refused; one it does is saved.
    await client.post(f"/form-questions/{banked[0].id}", data={"answer": "Sometimes", "back": "/applications"})
    session.refresh(banked[0])
    assert banked[0].answer is None
    await client.post(f"/form-questions/{banked[0].id}", data={"answer": "No", "back": "/applications"})
    await client.post(f"/form-questions/{banked[1].id}", data={"answer": "I like the work.", "back": "https://evil.example"})

    # A later form asking the same questions, worded with different punctuation, is filled from the bank.
    later = dict(
        FORM,
        fields=[*SCANNED["fields"][:4], {**SCANNED["fields"][4], "label": "Why do you want to work at Acme"}, DISABILITY],
    )
    plan = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=later)).json()
    assert (_steps(plan)["jaa-7"]["value"], _steps(plan)["jaa-7"]["source"]) == ("I like the work.", "bank")
    assert _steps(plan)["dis"]["value"] == "No"
    assert plan["missing"] == []
    assert "Questions from employers' forms" in (await client.get("/profile/apply-answers")).text


async def test_a_question_cannot_be_answered_logged_out(client: httpx.AsyncClient, session: Session) -> None:
    row, headers = await _ready(client, session, cv=False)
    await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=SCANNED)
    question = session.scalar(select(FormQuestion))
    await client.post("/logout")
    assert (await client.post(f"/form-questions/{question.id}", data={"answer": "x"})).status_code == 303
    session.refresh(question)
    assert question.answer is None


async def test_a_filled_form_with_more_pages_can_still_be_read(client: httpx.AsyncClient, session: Session) -> None:
    row, headers = await _ready(client, session, cv=False)
    row.status = ApplicationStatus.filled_awaiting_review.value
    session.flush()
    page_two = dict(SCANNED, fields=SCANNED["fields"][:2])
    response = await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=page_two)
    assert response.status_code == 200 and response.json()["missing"] == []


# Checks that need no AI.


def test_the_same_question_matches_however_it_is_punctuated() -> None:
    assert question_key("Why do you want to work at Acme?*") == question_key("why do you want to work at  ACME")


@pytest.mark.parametrize(
    "label",
    ["Do you have a disability?", "What is your ethnicity?", "Date of birth", "Salary expectations", "I confirm the above is true",
     "Have you any unspent criminal convictions?", "Do you require visa sponsorship?"],
)
def test_questions_only_the_person_may_answer(label: str) -> None:
    assert PERSON_ONLY.search(label)


def test_questions_the_ai_may_draft() -> None:
    for label in ("Why do you want to work here?", "Tell us about a project you are proud of", "Cover letter",
                  "Describe your experience with Python", "Tell us about yourself"):
        assert not PERSON_ONLY.search(label), label
        assert not STORY_QUESTION.search(label), label


@pytest.mark.parametrize(
    "label",
    ["Describe a time you debugged a difficult problem.", "Tell us about a time when you worked in a team",
     "Give an example of when you showed leadership", "Describe a situation where you had to meet a deadline",
     "Describe a challenge you faced and how you overcame it", "Please give examples of how you have handled conflict"],
)
def test_questions_asking_for_one_real_experience_are_left_for_the_person(label: str) -> None:
    # A CV almost never holds the story, so a draft would have to invent it.
    assert STORY_QUESTION.search(label)


async def test_a_story_question_is_never_sent_to_the_ai(session: Session, client: httpx.AsyncClient, claude: _Claude) -> None:
    story = {"label": "Describe a time you solved a difficult problem.", "name": "story", "type": "textarea", "required": True}
    _money(session, "2")
    row, headers = await _ready(client, session)

    plan = (await client.post(
        f"/extension/applications/{row.id}/form", headers=headers, json=dict(SCANNED, fields=[*SCANNED["fields"], story])
    )).json()

    assert "story" not in _steps(plan)
    assert story["label"] in plan["missing"]
    assert all("difficult problem" not in sent for sent in claude.sent)


@pytest.mark.parametrize(
    ("form_field", "answer", "ok"),
    [
        (FormField("Q", "q", "select", True, ["Yes", "No"]), "Yes", True),
        (FormField("Q", "q", "select", True, ["Yes", "No"]), "Maybe", False),
        (FormField("Q", "q", "textarea", True), "I worked at [Company] for years.", False),
        (FormField("Q", "q", "textarea", True), "I led the team — and more.", False),
        (FormField("Q", "q", "textarea", True), "x" * 1501, False),
        (FormField("Q", "q", "checkbox", True), "Yes", False),
        (FormField("Q", "q", "textarea", True), "I built a job search API.", True),
    ],
)
def test_drafts_that_are_unfinished_or_off_the_list_are_dropped(form_field: FormField, answer: str, ok: bool) -> None:
    assert _plausible(form_field, answer) is ok


def test_only_education_work_and_availability_answers_reach_the_ai() -> None:
    from app.applications.drafts import _saved_facts

    facts = _saved_facts(
        {"email": "a@b.c", "phone": "0700", "legal_first_name": "Mo", "criminal_convictions": False, "ethnicity": "White",
         "current_salary": "30000", "university": "University of Manchester", "notice_period": "1 month", "city": None}
    )
    assert facts == {"university": "University of Manchester", "notice_period": "1 month"}


async def test_reading_the_form_again_reuses_drafts_without_asking_again(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "2")
    row, headers = await _ready(client, session)
    optional = {"label": "Middle name", "name": "middle", "type": "text", "required": False}
    form = dict(SCANNED, fields=[*SCANNED["fields"], optional])
    await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=form)
    assert len(claude.sent) == 2
    # An optional field that doesn't ask for the person's own words is not drafted.
    assert "Middle name" not in claude.sent[0]

    again = (await client.post(f"/extension/applications/{row.id}/form", headers=headers, json=form)).json()

    assert len(claude.sent) == 2
    assert (_steps(again)["jaa-7"]["value"], _steps(again)["jaa-7"]["source"]) == (WHY, "ai")
    session.refresh(row)
    assert row.fill_plan_json["drafts"][question_key("Why do you want to work at Acme?")]["value"] == WHY
