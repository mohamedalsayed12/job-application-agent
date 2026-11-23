"""Stage 1 of the sign-up flow: register first, then a wizard that reads the CV and saves each step."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.applicants.apply_answers import answers_as_map
from app.applicants.cv_read import grounded_contact
from app.applicants.service import create_applicant, parse_preference_input
from app.applications.mapper import build_fill_plan, match_answer_key
from app.applications.schema import FormField, FormSchema
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    PREFER_NOT,
    Applicant,
    ApplicantPreference,
    ApplicantSkill,
    ApplicationAnswers,
    CvFile,
)
from tests.auth import sign_up
from tests.test_cv_read import FOUND, _Claude, _docx, _money

EMAIL = "new@example.com"
PASSWORD = "correct horse battery"
CV_LINES = [
    "Sam Example",
    "Manchester M1 4BT | 07700 900123 | sam@example.com",
    "linkedin.com/in/sam-example | github.com/samexample",
    "BSc Business Management, University of Manchester, 2027",
    "Skills: Python, FastAPI, PostgreSQL & SQL",
    "Projects: built a job search API in Python",
]
CONTACT = {
    "first_name": "Sam",
    "last_name": "Example",
    "phone": "07700 900123",
    "city": "Manchester",
    "postcode": "M1 4BT",
    "linkedin_url": "linkedin.com/in/sam-example",
    "github_url": "github.com/samexample",
    "portfolio_url": "",
}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DETAILS = {
    "legal_first_name": "Sam",
    "legal_last_name": "Example",
    "phone": "07700 900123",
    "city": "Manchester",
    "postcode": "M1 4BT",
    "country": "United Kingdom",
    "linkedin_url": "https://linkedin.com/in/sam-example",
    "right_to_work_uk": "yes",
    "needs_visa_sponsorship": "no",
    "nationality": "British",
}
PREFERENCES = {
    "target_roles": "Junior Python Developer",
    "locations": "Manchester",
    "experience_level": "entry",
    "skills": "Python\nFastAPI",
}
AVAILABILITY = {
    "notice_period": "2 weeks",
    "earliest_start_date": "2027-07-01",
    "willing_to_relocate": "no",
    "work_hours": "Full-time",
    "work_location": "Hybrid",
}


@pytest.fixture
def claude() -> _Claude:
    return _Claude({**FOUND, "contact": CONTACT})


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
        gateway = claude.gateway()
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


def _applicant(session: Session, email: str = EMAIL) -> Applicant:
    applicant = session.scalar(select(Applicant).where(Applicant.email == email))
    session.refresh(applicant)
    return applicant


def _answers(session: Session) -> ApplicationAnswers | None:
    row = session.scalar(select(ApplicationAnswers))
    if row is not None:
        session.refresh(row)
    return row


async def _register(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await sign_up(client, email, PASSWORD)


async def _upload(client: httpx.AsyncClient, lines: list[str] = CV_LINES) -> httpx.Response:
    return await client.post("/onboarding/cv", files={"cv": ("cv.docx", _docx(lines), DOCX)})


async def _through_cv(client: httpx.AsyncClient, session: Session) -> httpx.Response:
    _money(session, "2")
    assert (await _register(client)).status_code == 303
    return await _upload(client)


async def _finish(client: httpx.AsyncClient, session: Session) -> httpx.Response:
    await _through_cv(client, session)
    assert (await client.post("/onboarding/details", data=DETAILS)).status_code == 303
    assert (await client.post("/onboarding/preferences", data=PREFERENCES)).status_code == 303
    assert (await client.post("/onboarding/availability", data=AVAILABILITY)).status_code == 303
    return await client.post("/onboarding/diversity", data={"religion": "No religion"})


def _input_value(page: str, name: str) -> str | None:
    found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', page)
    return found.group(1) if found else None


# The first screen.


async def test_a_visitor_sees_the_register_form_first_with_a_way_to_log_in(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert 'action="/register"' in page.text
    assert 'Already have an account? <a href="/login">Log in</a>' in page.text


async def test_the_login_page_links_back_to_register(client: httpx.AsyncClient) -> None:
    page = await client.get("/login")
    assert '<a href="/register">Create an account</a>' in page.text


async def test_registering_starts_the_wizard_at_the_cv_step(client: httpx.AsyncClient, session: Session) -> None:
    response = await _register(client)
    assert response.headers["location"] == "/onboarding"
    assert (await client.get("/onboarding")).headers["location"] == "/onboarding/cv"
    assert (await client.get("/")).headers["location"] == "/onboarding/cv"
    assert _applicant(session).onboarding_step == "cv"
    page = await client.get("/onboarding/cv")
    assert "Step 2 of 6" in page.text and "Upload your CV" in page.text


async def test_accounts_made_before_the_wizard_never_see_it(client: httpx.AsyncClient, session: Session) -> None:
    preference = parse_preference_input(target_roles=["Backend Developer"], locations=["Manchester"])
    applicant = create_applicant(session, email="old@example.com", preference=preference)
    session.flush()
    assert applicant.onboarding_step is None


# The happy path.


async def test_the_cv_fills_contact_details_only_from_what_it_says(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    upload = await _through_cv(client, session)
    assert upload.status_code == 303
    assert upload.headers["location"] == "/onboarding/details?cv=read"
    assert claude.calls == 1

    page = await client.get(upload.headers["location"])
    assert "Check each answer marked" in page.text
    assert _input_value(page.text, "legal_first_name") == "Sam"
    assert _input_value(page.text, "legal_last_name") == "Example"
    assert _input_value(page.text, "phone") == "07700 900123"
    assert _input_value(page.text, "postcode") == "M1 4BT"
    assert _input_value(page.text, "linkedin_url") == "https://linkedin.com/in/sam-example"
    assert _input_value(page.text, "github_url") == "https://github.com/samexample"
    assert _input_value(page.text, "portfolio_url") == ""
    assert page.text.count('class="from-cv"') == 7
    # Right to work is never taken from the CV, so neither answer is chosen in advance.
    assert '<option value="yes" selected>' not in page.text
    # Nothing is saved until the person presses Continue.
    assert _answers(session) is None


async def test_the_whole_wizard_saves_every_step_then_opens_the_matches(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    details = await client.post("/onboarding/details", data=DETAILS)
    assert details.headers["location"] == "/onboarding/preferences"

    preferences_page = await client.get("/onboarding/preferences")
    assert "Junior Python Developer\nGraduate Software Engineer</textarea>" in preferences_page.text
    assert "From your CV we filled in" in preferences_page.text
    preferences = await client.post("/onboarding/preferences", data=PREFERENCES)
    assert preferences.headers["location"] == "/onboarding/availability"

    availability = await client.post("/onboarding/availability", data=AVAILABILITY)
    assert availability.headers["location"] == "/onboarding/diversity"
    diversity_page = await client.get("/onboarding/diversity")
    assert diversity_page.text.count('value="prefer_not_to_say" selected') == 10

    done = await client.post("/onboarding/diversity", data={"religion": "No religion"})
    assert done.headers["location"] == "/listings"

    applicant = _applicant(session)
    assert applicant.onboarding_step is None
    answers = _answers(session)
    assert (answers.legal_first_name, answers.phone, answers.right_to_work_uk, answers.needs_visa_sponsorship) == (
        "Sam",
        "07700 900123",
        True,
        False,
    )
    assert (answers.notice_period, answers.earliest_start_date, answers.willing_to_relocate) == ("2 weeks", date(2027, 7, 1), False)
    assert (answers.work_hours, answers.work_location) == ("Full-time", "Hybrid")
    assert answers.religion == "No religion"
    assert answers.sexual_orientation == answers.school_type == answers.gender == PREFER_NOT
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant.id))
    assert preference.target_roles == ["Junior Python Developer"]
    skills = session.scalars(select(ApplicantSkill.text_as_typed).where(ApplicantSkill.applicant_id == applicant.id)).all()
    assert sorted(skills) == ["FastAPI", "Python"]
    # What the CV read is dropped once the person has checked and saved it.
    cv = session.scalar(select(CvFile))
    session.refresh(cv)
    assert cv.suggestions is None


async def test_a_finished_person_goes_to_their_matches_not_back_into_the_wizard(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _finish(client, session)
    for path in ("/", "/onboarding", "/onboarding/cv", "/onboarding/diversity"):
        assert (await client.get(path)).headers["location"] == "/listings", path
    await client.post("/logout")
    login = await client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert login.headers["location"] == "/listings"


# Moving through the steps.


async def test_later_steps_stay_shut_until_reached(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    for step in ("details", "preferences", "availability", "diversity"):
        assert (await client.get(f"/onboarding/{step}")).headers["location"] == "/onboarding/cv", step
    skipped = await client.post("/onboarding/availability", data=AVAILABILITY)
    assert skipped.headers["location"] == "/onboarding/cv"
    assert _answers(session) is None
    assert _applicant(session).onboarding_step == "cv"


async def test_logging_in_again_opens_the_step_reached(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/logout")
    login = await client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert login.headers["location"] == "/onboarding"
    assert (await client.get("/onboarding")).headers["location"] == "/onboarding/preferences"


async def test_going_back_shows_what_was_saved_and_keeps_the_place_reached(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data={**DETAILS, "portfolio_url": ""})
    await client.post("/onboarding/preferences", data=PREFERENCES)

    back = await client.get("/onboarding/details")
    assert back.status_code == 200
    assert _input_value(back.text, "nationality") == "British"
    assert 'class="from-cv"' not in back.text
    resaved = await client.post("/onboarding/details", data={**DETAILS, "phone": "07700 900999"})
    assert resaved.headers["location"] == "/onboarding/preferences"
    assert _applicant(session).onboarding_step == "availability"
    assert _answers(session).phone == "07700 900999"
    progress = (await client.get("/onboarding/availability")).text
    assert '<a href="/onboarding/details">' in progress and 'aria-current="step">5. Availability' in progress


async def test_a_cv_already_uploaded_can_be_kept(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    await _through_cv(client, session)
    page = await client.get("/onboarding/cv")
    assert "Continue with this CV" in page.text
    kept = await client.post("/onboarding/cv/keep")
    assert kept.headers["location"] == "/onboarding/details"
    assert claude.calls == 1


async def test_continuing_without_a_cv_is_refused(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    refused = await client.post("/onboarding/cv/keep")
    assert refused.status_code == 400 and "Upload your CV first." in refused.text
    assert _applicant(session).onboarding_step == "cv"


# When things go wrong.


async def test_a_file_that_isnt_a_cv_is_refused_and_the_wizard_waits(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    refused = await client.post("/onboarding/cv", files={"cv": ("cv.txt", b"just some text", "text/plain")})
    assert refused.status_code == 400
    assert "Upload a PDF or a Word document" in refused.text
    assert _applicant(session).onboarding_step == "cv"
    assert session.scalar(select(CvFile)) is None


async def test_without_ai_the_cv_is_kept_and_the_person_fills_in_the_form(
    client: httpx.AsyncClient, session: Session, claude: _Claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    await _register(client)
    upload = await _upload(client)
    assert upload.headers["location"] == "/onboarding/details?cv=not_configured"
    page = await client.get(upload.headers["location"])
    assert "Reading CVs isn&#x27;t set up" in page.text or "Reading CVs isn't set up" in page.text
    assert _input_value(page.text, "legal_first_name") == ""
    assert claude.calls == 0
    assert session.scalar(select(CvFile)) is not None


async def test_with_no_money_left_the_cv_is_not_sent_but_the_wizard_goes_on(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "0")
    await _register(client)
    upload = await _upload(client)
    assert upload.headers["location"] == "/onboarding/details?cv=budget"
    assert "AI budget is used up" in (await client.get(upload.headers["location"])).text
    assert claude.calls == 0


async def test_with_no_spending_cap_set_the_page_says_reading_is_not_set_up(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    """No cap at all is a setup gap, not a spent budget, so the page must not claim money ran out."""
    await _register(client)
    upload = await _upload(client)
    assert upload.headers["location"] == "/onboarding/details?cv=not_configured"
    page = (await client.get(upload.headers["location"])).text
    assert "budget is used up" not in page
    assert claude.calls == 0


async def test_contact_details_the_cv_doesnt_have_are_never_filled_in(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    claude.payload = {
        **FOUND,
        "contact": {
            **CONTACT,
            "last_name": "Smith",
            "phone": "07999 123456",
            "linkedin_url": "linkedin.com/in/someone-else",
            "portfolio_url": "github.com/samexample",
        },
    }
    upload = await _through_cv(client, session)
    page = await client.get(upload.headers["location"])
    assert _input_value(page.text, "legal_first_name") == "Sam"
    assert _input_value(page.text, "legal_last_name") == ""
    assert _input_value(page.text, "phone") == ""
    assert _input_value(page.text, "linkedin_url") == ""
    assert _input_value(page.text, "portfolio_url") == ""


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"right_to_work_uk": ""}, "Answer both right-to-work questions."),
        ({"needs_visa_sponsorship": "maybe"}, "Answer both right-to-work questions."),
        ({"legal_last_name": " "}, "Enter your legal first and last name."),
        ({"phone": ""}, "Enter your phone number."),
        ({"city": ""}, "Enter your town or city."),
        ({"linkedin_url": "linkedin.com/in/sam"}, "Links must start with http:// or https://."),
    ],
)
async def test_bad_details_are_refused_keep_what_was_typed_and_save_nothing(
    client: httpx.AsyncClient, session: Session, change: dict[str, str], message: str
) -> None:
    await _through_cv(client, session)
    refused = await client.post("/onboarding/details", data={**DETAILS, "nationality": "Typed nationality", **change})
    assert refused.status_code == 400
    assert message in refused.text
    assert _input_value(refused.text, "nationality") == "Typed nationality"
    assert _answers(session) is None
    assert _applicant(session).onboarding_step == "details"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"notice_period": ""}, "Only the start date is optional."),
        ({"willing_to_relocate": ""}, "Only the start date is optional."),
        ({"notice_period": "Whenever"}, "Choose one of the options for notice period."),
        ({"earliest_start_date": "next summer"}, "Enter the earliest start date as a date."),
    ],
)
async def test_bad_availability_is_refused(
    client: httpx.AsyncClient, session: Session, change: dict[str, str], message: str
) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/onboarding/preferences", data=PREFERENCES)
    refused = await client.post("/onboarding/availability", data={**AVAILABILITY, **change})
    assert refused.status_code == 400 and message in refused.text
    assert _answers(session).notice_period is None
    assert _applicant(session).onboarding_step == "availability"


async def test_a_start_date_is_optional(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/onboarding/preferences", data=PREFERENCES)
    saved = await client.post("/onboarding/availability", data={**AVAILABILITY, "earliest_start_date": ""})
    assert saved.headers["location"] == "/onboarding/diversity"
    assert _answers(session).earliest_start_date is None


async def test_bad_preferences_are_refused_and_the_wizard_waits(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    refused = await client.post("/onboarding/preferences", data={**PREFERENCES, "target_roles": ""})
    assert refused.status_code == 400
    assert _applicant(session).onboarding_step == "preferences"


async def test_what_a_person_types_in_the_wizard_is_escaped(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    refused = await client.post("/onboarding/details", data={**DETAILS, "nationality": '<b>bold"', "phone": ""})
    assert "<b>bold" not in refused.text
    assert "&lt;b&gt;bold&quot;" in refused.text


async def test_the_wizard_needs_a_login(client: httpx.AsyncClient) -> None:
    for path in ("/onboarding", "/onboarding/cv", "/onboarding/details"):
        assert (await client.get(path)).headers["location"] == "/login", path
    assert (await client.post("/onboarding/details", data=DETAILS)).headers["location"] == "/login"


async def test_replacing_the_cv_drops_what_was_read_from_the_old_one(client: httpx.AsyncClient, session: Session) -> None:
    await _through_cv(client, session)
    files = {"cv": ("cv.pdf", b"%PDF-1.7\nmade up contents of a CV", "application/pdf")}
    await client.post("/profile/cv", files=files)
    cv = session.scalar(select(CvFile))
    session.refresh(cv)
    assert cv.suggestions is None


async def test_deleting_the_account_removes_the_new_answers(client: httpx.AsyncClient, session: Session) -> None:
    await _finish(client, session)
    deleted = await client.post("/account/delete", data={"password": PASSWORD})
    assert deleted.status_code == 200
    assert session.scalar(select(ApplicationAnswers)) is None
    assert session.scalar(select(CvFile)) is None


# The answers page shows the new questions too.


async def test_the_answers_page_shows_and_saves_the_new_questions(client: httpx.AsyncClient, session: Session) -> None:
    await _finish(client, session)
    page = await client.get("/profile/apply-answers")
    assert '<option value="2 weeks" selected>' in page.text
    assert '<option value="No religion" selected>' in page.text
    form = {
        **DETAILS,
        **AVAILABILITY,
        "notice_period": "1 month",
        "school_type": "A school outside the UK",
        "heard_about": "Job board",
        "previously_employed": "no",
    }
    saved = await client.post("/profile/apply-answers", data=form)
    assert saved.status_code == 303
    answers = _answers(session)
    assert (answers.notice_period, answers.school_type) == ("1 month", "A school outside the UK")
    # A diversity answer left out of the form goes back to prefer not to say rather than keeping an old answer.
    assert answers.religion == PREFER_NOT


async def test_an_answer_typed_before_the_choices_existed_still_shows(client: httpx.AsyncClient, session: Session) -> None:
    await _finish(client, session)
    row = _answers(session)
    row.gender = "Genderfluid"
    session.flush()
    page = await client.get("/profile/apply-answers")
    assert '<option value="Genderfluid" selected>Genderfluid</option>' in page.text


# The form filler knows the new questions.


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("What is your notice period?", "notice_period"),
        ("When can you start?", "earliest_start_date"),
        ("Earliest start date", "earliest_start_date"),
        ("Are you willing to relocate?", "willing_to_relocate"),
        ("Are you looking for full-time or part-time work?", "work_hours"),
        ("What is your sexual orientation?", "sexual_orientation"),
        ("Religion or belief", "religion"),
        ("Do you have caring responsibilities?", "caring_responsibilities"),
        ("What type of school did you attend between 11 and 16?", "school_type"),
        ("What was the occupation of your main household earner when you were 14?", "parental_occupation"),
        ("Did you receive free school meals?", "free_school_meals"),
        ("Socio-economic background", "free_school_meals"),
        ("Gender", "gender"),
    ],
)
def test_the_form_filler_recognises_the_new_questions(label: str, key: str) -> None:
    assert match_answer_key(label) == key


@pytest.mark.parametrize(
    "label",
    [
        "Start date",
        "I confirm this is true to the best of my knowledge and belief",
        "I make this declaration in good faith",
    ],
)
def test_the_form_filler_leaves_lookalike_questions_alone(label: str) -> None:
    assert match_answer_key(label) is None


def test_saved_availability_fills_the_form() -> None:
    row = ApplicationAnswers(
        notice_period="1 month",
        earliest_start_date=date(2027, 7, 1),
        willing_to_relocate=True,
        sexual_orientation=PREFER_NOT,
    )
    for key in ("gender", "ethnicity", "disability", "veteran", "free_school_meals", "religion", "caring_responsibilities", "school_type", "parental_occupation"):
        setattr(row, key, PREFER_NOT)
    schema = FormSchema(
        ats_kind="unknown", source="stub",
        fields=[
            FormField(label="Notice period", name="notice", field_type="text", required=True),
            FormField(label="Earliest start date", name="start", field_type="date", required=True),
            FormField(label="Willing to relocate?", name="move", field_type="select", required=True, options=["Yes", "No"]),
            FormField(label="Sexual orientation", name="so", field_type="select", required=False, options=["Gay", "Prefer not to say"]),
        ],
    )
    plan = build_fill_plan(schema, answers_as_map(row, email="a@example.com"))
    assert [(step.name, step.value) for step in plan.steps] == [
        ("notice", "1 month"),
        ("start", "2027-07-01"),
        ("move", "Yes"),
        ("so", "Prefer not to say"),
    ]
    assert plan.missing == []


@pytest.mark.parametrize(
    ("saved", "options", "picked"),
    [
        ("Available now", ["Immediately", "1 month", "3 months"], "Immediately"),
        ("Available now", ["None", "1 week"], "None"),
        ("2 weeks", ["1 week", "Two weeks", "1 month"], "Two weeks"),
        ("1 month", ["2 weeks", "4 weeks", "8 weeks"], "4 weeks"),
        ("3 months or more", ["1 month", "3 months+"], "3 months+"),
        ("Full-time", ["Full time", "Part time"], "Full time"),
        ("Either", ["Full-time", "Part-time", "No preference"], "No preference"),
        ("On-site", ["Office-based", "Hybrid", "Remote"], "Office-based"),
        ("Remote", ["In office", "Fully remote"], "Fully remote"),
        ("Hybrid", ["hybrid", "Remote"], "hybrid"),
    ],
)
def test_saved_answers_pick_the_employers_wording(saved: str, options: list[str], picked: str) -> None:
    from app.applications.mapper import _pick_option

    assert _pick_option(options, saved) == picked


@pytest.mark.parametrize(
    ("saved", "options"),
    [
        # A near miss is left for the applicant, never guessed.
        ("1 month", ["Less than 1 month", "More than 1 month"]),
        ("2 weeks", ["1 week", "1 month"]),
        ("Remote", ["On-site", "Hybrid"]),
        ("Either", ["Full-time", "Part-time"]),
    ],
)
def test_no_close_wording_is_left_for_the_applicant(saved: str, options: list[str]) -> None:
    from app.applications.mapper import _pick_option

    assert _pick_option(options, saved) is None


# Reading contact details from CV text.


def test_contact_links_must_be_on_the_right_site() -> None:
    text = "Sam Example linkedin.com/in/sam github.com/sam https://sam.dev"
    found = grounded_contact(
        {"linkedin_url": "github.com/sam", "github_url": "https://github.com/sam", "portfolio_url": "linkedin.com/in/sam"},
        text,
    )
    assert (found.linkedin_url, found.github_url, found.portfolio_url) == ("", "https://github.com/sam", "")
    assert grounded_contact({"portfolio_url": "https://sam.dev"}, text).portfolio_url == "https://sam.dev"


def test_a_phone_number_counts_whatever_its_spacing() -> None:
    text = "Call +44 (0)7700-900-123"
    assert grounded_contact({"phone": "+44 0 7700 900 123"}, text).phone == "+44 0 7700 900 123"
    assert grounded_contact({"phone": "123"}, "123 Main St").phone == ""


def test_an_email_address_is_not_a_link() -> None:
    assert grounded_contact({"portfolio_url": "sam@example.com"}, "sam@example.com").portfolio_url == ""


def test_a_malformed_contact_block_reads_as_nothing() -> None:
    assert grounded_contact("Sam", "Sam") == grounded_contact(None, "")
