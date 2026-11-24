"""D73: the extra questions employers' forms ask, with the choices UK forms use, saved once and used to fill forms."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import Session

from app.applicants.apply_answers import (
    AnswersInput,
    AnswersRefused,
    answers_as_map,
    save_answers,
)
from app.applicants.cv_read import grounded_education
from app.applicants.questions import EXTRA_QUESTIONS
from app.applications.mapper import build_fill_plan, match_answer_key
from app.applications.schema import FormField, FormSchema
from app.models import PREFER_NOT, ApplicationAnswers
from tests.test_cv_read import FOUND, _Claude
from tests.test_onboarding import (  # noqa: F401  (fixtures)
    AVAILABILITY,
    CONTACT,
    DETAILS,
    PREFERENCES,
    _answers,
    _applicant,
    _register,
    _through_cv,
    _upload,
    client,  # noqa: F401
)
from tests.test_onboarding import _money as _cap

CV = [
    "Sam Example",
    "Manchester M1 4BT | 07700 900123 | sam@example.com",
    "University of Manchester, BSc (Hons) Business Management, predicted 2:1, expected June 2027",
    "A levels: Maths A, Business B, Economics B",
    "Barista, Costa Coffee, 2024 to now",
    "Skills: Python, FastAPI, PostgreSQL",
]
EDUCATION = {
    "university": "University of Manchester",
    "degree_title": "BSc (Hons) Business Management",
    "degree_grade": "Upper second class (2:1)",
    "graduation_date": "2027-06",
    "a_levels": "Maths A, Business B, Economics B",
    "current_job_title": "Barista",
    "current_employer": "Costa Coffee",
}


@pytest.fixture
def claude() -> _Claude:
    return _Claude({**FOUND, "contact": CONTACT, "education": EDUCATION})


def _form(answers: ApplicationAnswers, *fields: FormField) -> list[tuple[str, str]]:
    schema = FormSchema(ats_kind="unknown", source="stub", fields=list(fields))
    plan = build_fill_plan(schema, answers_as_map(answers, email="sam@example.com"))
    return [(step.name, step.value) for step in plan.steps]


def _row(**extra: str) -> ApplicationAnswers:
    row = ApplicationAnswers(extra=extra)
    for key in ("gender", "ethnicity", "disability", "veteran", "free_school_meals", "sexual_orientation", "religion",
                "caring_responsibilities", "school_type", "parental_occupation"):
        setattr(row, key, PREFER_NOT)
    return row


# The wizard.


async def test_the_details_step_needs_the_kind_of_right_to_work(client: httpx.AsyncClient, session: Session) -> None:  # noqa: F811
    await _through_cv(client, session)
    refused = await client.post("/onboarding/details", data={**DETAILS, "extra_right_to_work_type": ""})
    assert refused.status_code == 400 and "Choose what gives you the right to work in the UK." in refused.text

    saved = await client.post(
        "/onboarding/details", data={**DETAILS, "extra_right_to_work_type": "Graduate visa", "extra_visa_expiry_date": "2029-01-31", "extra_title": "Mx"}
    )
    assert saved.headers["location"] == "/onboarding/preferences"
    assert _answers(session).extra == {"right_to_work_type": "Graduate visa", "visa_expiry_date": "2029-01-31", "title": "Mx"}


async def test_the_education_step_is_filled_from_the_cv_and_the_profile(client: httpx.AsyncClient, session: Session) -> None:  # noqa: F811
    _cap(session, "2")
    assert (await _register(client)).status_code == 303
    await _upload(client, CV)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/onboarding/preferences", data={**PREFERENCES, "salary_amount": "28000"})

    page = (await client.get("/onboarding/education")).text

    assert "We filled in what your CV shows" in page
    assert 'value="University of Manchester"' in page
    assert '<option value="Upper second class (2:1)" selected>' in page
    assert 'name="extra_graduation_date" id="extra_graduation_date" type="month" placeholder="YYYY-MM" value="2027-06"' in page
    assert 'value="Costa Coffee"' in page
    # The salary comes from the profile, not the CV.
    assert 'name="extra_salary_expectation" id="extra_salary_expectation" type="text" value="28000"' in page
    assert page.count('class="from-cv"') == 7


async def test_the_education_step_saves_what_is_sent_and_refuses_what_is_not_a_choice(
    client: httpx.AsyncClient, session: Session  # noqa: F811
) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/onboarding/preferences", data=PREFERENCES)

    bad_grade = await client.post("/onboarding/education", data={"extra_degree_grade": "A 2:1, honestly"})
    assert bad_grade.status_code == 400 and "Choose one of the options for" in bad_grade.text
    bad_month = await client.post("/onboarding/education", data={"extra_graduation_date": "June"})
    assert bad_month.status_code == 400 and "as a month and year" in bad_month.text
    assert _applicant(session).onboarding_step == "education"

    saved = await client.post(
        "/onboarding/education",
        data={"extra_highest_qualification": "Bachelor's degree", "extra_graduation_date": "2027-06", "extra_currently_studying": "yes"},
    )
    assert saved.headers["location"] == "/onboarding/availability"
    assert _answers(session).extra == {
        "right_to_work_type": "British or Irish citizen",
        "highest_qualification": "Bachelor's degree",
        "graduation_date": "2027-06",
        "currently_studying": "yes",
    }


async def test_screening_and_diversity_extras_are_saved_on_their_steps(client: httpx.AsyncClient, session: Session) -> None:  # noqa: F811
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    await client.post("/onboarding/preferences", data=PREFERENCES)
    await client.post("/onboarding/education", data={})
    page = (await client.get("/onboarding/availability")).text
    assert "Questions most forms ask" in page and "Are you 18 or over?" in page

    await client.post(
        "/onboarding/availability",
        data={**AVAILABILITY, "extra_heard_about_source": "LinkedIn", "extra_over_18": "yes", "extra_criminal_convictions": "no"},
    )
    diversity = (await client.get("/onboarding/diversity")).text
    assert "White: English, Welsh, Scottish, Northern Irish or British" in diversity
    assert '<option value="Female">Female</option>' in diversity
    await client.post("/onboarding/diversity", data={"extra_age_band": "16 to 24"})

    extra = _answers(session).extra
    assert (extra["heard_about_source"], extra["over_18"], extra["criminal_convictions"], extra["age_band"]) == (
        "LinkedIn", "yes", "no", "16 to 24",
    )


async def test_the_answers_page_shows_every_group_and_keeps_the_old_heard_about(
    client: httpx.AsyncClient, session: Session  # noqa: F811
) -> None:
    await _through_cv(client, session)
    await client.post("/onboarding/details", data=DETAILS)
    row = _answers(session)
    row.heard_about = "A friend"
    session.flush()

    page = (await client.get("/profile/apply-answers")).text
    for title in ("More about you", "Education", "Work experience and pay", "Questions most forms ask", "Driving licence"):
        assert title in page, title
    saved = await client.post(
        "/profile/apply-answers", data={**DETAILS, **AVAILABILITY, "extra_driving_licence": "Full UK driving licence"}
    )
    assert saved.status_code == 303
    row = _answers(session)
    assert row.heard_about == "A friend"
    assert row.extra["driving_licence"] == "Full UK driving licence"
    assert answers_as_map(row, email="a@example.com")["heard_about"] == "A friend"


# Saving.


def test_blank_and_unknown_extras_are_left_out(session: Session, applicant_id) -> None:  # noqa: ANN001
    row = save_answers(session, applicant_id, AnswersInput(extra={"university": "  ", "made_up": "x", "over_18": "YES"}))
    assert row.extra == {"over_18": "yes"}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"over_18": "maybe"}, "Choose yes or no"),
        ({"date_of_birth": "2999-01-01"}, "past date"),
        ({"visa_expiry_date": "soon"}, "as a date"),
        ({"adjustments_details": "x" * 501}, "under 500 characters"),
    ],
)
def test_bad_extras_are_refused(session: Session, applicant_id, extra: dict[str, str], message: str) -> None:  # noqa: ANN001
    with pytest.raises(AnswersRefused, match=message):
        save_answers(session, applicant_id, AnswersInput(extra=extra))


@pytest.fixture
def applicant_id(session: Session):  # noqa: ANN201
    from app.applicants.service import create_applicant, parse_preference_input

    preference = parse_preference_input(target_roles=["Analyst"], locations=["Leeds"])
    applicant = create_applicant(session, email="q@example.com", preference=preference)
    session.flush()
    return applicant.id


# Reading the CV.


def test_education_is_kept_only_when_the_cv_says_it() -> None:
    text = "\n".join(CV)
    assert grounded_education(EDUCATION, text).degree_grade == "Upper second class (2:1)"
    made_up = grounded_education(
        {**EDUCATION, "university": "University of Leeds", "degree_grade": "First class (1st)", "graduation_date": "2027-07"},
        text,
    )
    assert (made_up.university, made_up.degree_grade, made_up.graduation_date) == ("", "", "")
    assert grounded_education("not an object", text).university == ""


# Filling employers' forms.


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Title", "title"),
        ("Date of birth", "date_of_birth"),
        ("What type of visa do you currently hold?", "right_to_work_type"),
        ("When does your current visa expire?", "visa_expiry_date"),
        ("Do you have the right to work in the UK?", "right_to_work_uk"),
        ("University", "university"),
        ("Degree classification", "degree_grade"),
        ("Expected graduation date", "graduation_date"),
        ("How did you hear about this role?", "heard_about"),
        ("Do you require any reasonable adjustments for your interview?", "reasonable_adjustments"),
        ("Please give details of the adjustments you need", "adjustments_details"),
        ("Do you have any unspent criminal convictions?", "criminal_convictions"),
        ("Address line 2", "address_line_2"),
        ("Address line 1", "address_line"),
        ("Is your gender identity the same as the sex you were assigned at birth?", "gender_same_as_birth"),
        ("Gender", "gender"),
        ("Did either of your parents attend university?", "parents_university"),
        ("Marital status", "marital_status"),
        ("Are you related to anyone who works for the company?", "relatives_at_company"),
        ("Have you previously worked for us?", "previously_employed"),
        ("I have read and accept the privacy notice", "agree_privacy_notice"),
        ("Salary expectations", "salary_expectation"),
    ],
)
def test_the_form_filler_recognises_the_extra_questions(label: str, key: str) -> None:
    assert match_answer_key(label) == key


def test_every_extra_question_is_recognised_by_its_own_label_or_a_pattern() -> None:
    # A question nobody can find on a form would be asked for nothing.
    assert all(question.patterns for question in EXTRA_QUESTIONS)


def test_extras_fill_in_the_forms_own_format() -> None:
    row = _row(graduation_date="2027-06", date_of_birth="2004-03-09", over_18="yes", heard_about_source="LinkedIn")
    assert _form(
        row,
        FormField("Graduation date", "grad_text", "text", True),
        FormField("Expected graduation", "grad_month", "month", True),
        FormField("Graduation year", "grad_year", "select", True, ["2026", "2027", "2028"]),
        FormField("Date of birth", "dob", "text", False),
        FormField("Are you 18 or over?", "adult", "select", True, ["Yes", "No"]),
        FormField("How did you hear about us?", "source", "select", True, ["Indeed", "LinkedIn", "Other"]),
        FormField("Age group", "age", "select", False, ["16-24", "25-34", "Prefer not to say"]),
    ) == [
        ("grad_text", "06/2027"),
        ("grad_month", "2027-06"),
        ("grad_year", "2027"),
        ("dob", "09/03/2004"),
        ("adult", "Yes"),
        ("source", "LinkedIn"),
        ("age", "Prefer not to say"),
    ]


@pytest.mark.parametrize(
    ("saved", "options", "picked"),
    [
        ("Asian or Asian British: Indian", ["Asian or Asian British - Indian", "White"], "Asian or Asian British - Indian"),
        ("Asian or Asian British: Indian", ["Indian", "Pakistani"], "Indian"),
        ("Asian or Asian British: Indian", ["Asian - Indian", "Asian - Other"], "Asian - Indian"),
        ("Asian or Asian British: Indian", ["Asian/Asian British", "White"], "Asian/Asian British"),
        ("White: English, Welsh, Scottish, Northern Irish or British", ["White British", "White Irish"], "White British"),
        ("White: Any other White background", ["White British", "White - Other"], "White - Other"),
    ],
)
def test_a_census_ethnic_group_finds_the_forms_wording(saved: str, options: list[str], picked: str) -> None:
    row = _row()
    row.ethnicity = saved
    assert _form(row, FormField("Ethnicity", "eth", "select", False, options)) == [("eth", picked)]


@pytest.mark.parametrize(
    ("column", "saved", "options", "picked"),
    [
        ("gender", "Female", ["Woman", "Man", "Prefer not to say"], "Woman"),
        ("gender", "Woman", ["Female", "Male"], "Female"),
        ("sexual_orientation", "Straight or heterosexual", ["Heterosexual/Straight", "Gay/Lesbian"], "Heterosexual/Straight"),
        ("sexual_orientation", "Gay or lesbian", ["Gay / Lesbian", "Bisexual"], "Gay / Lesbian"),
    ],
)
def test_census_wording_matches_older_wording(column: str, saved: str, options: list[str], picked: str) -> None:
    row = _row()
    setattr(row, column, saved)
    label = "Gender" if column == "gender" else "Sexual orientation"
    assert _form(row, FormField(label, "f", "select", False, options)) == [("f", picked)]


def test_a_choice_the_form_does_not_offer_is_left_for_the_person() -> None:
    row = _row(driving_licence="Full UK driving licence")
    schema = FormSchema(ats_kind="unknown", source="stub", fields=[FormField("Driving licence", "dl", "select", True, ["Yes", "No"])])
    plan = build_fill_plan(schema, answers_as_map(row, email="a@example.com"))
    assert (plan.steps, plan.missing) == ([], ["Driving licence"])


def test_nothing_saved_leaves_extras_blank_and_diversity_at_prefer_not(session: Session) -> None:
    answers = answers_as_map(None, email="a@example.com")
    assert answers["university"] is None and answers["over_18"] is None
    assert answers["age_band"] == PREFER_NOT
