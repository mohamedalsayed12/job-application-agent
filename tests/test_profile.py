"""Step 18: the profile (T30), CV upload (T31), account deletion (T32) and the Internship level."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.applicants.cv import MAX_CV_BYTES, MAX_UPLOAD_BODY_BYTES, CvRefused, check_cv
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.matching.match import match_role
from app.models import (
    Applicant,
    ApplicantCertification,
    ApplicantJobMatch,
    ApplicantPreference,
    ApplicantProfile,
    ApplicantSkill,
    CvFile,
    CvFileType,
    ExperienceLevel,
    SearchRun,
    SearchRunStatus,
    SearchTemplate,
    SearchTemplateStatus,
)

EMAIL = "person@example.com"
OTHER_EMAIL = "other@example.com"
PASSWORD = "correct horse battery"
PDF = b"%PDF-1.7\nmade up contents of a CV"
WORD_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PROFILE = {
    "target_roles": "Backend Developer\nSoftware Engineer",
    "locations": "Manchester\nRemote",
    "experience_level": "entry",
    "travel_distance": "25",
    "salary_amount": "32000",
    "salary_currency": "gbp",
    "salary_period": "year",
    "skills": "Python\nSQL\npython\n",
    "cert_name": ["AWS Cloud Practitioner", "Driving licence", ""],
    "cert_issuer": ["Amazon", "DVLA", ""],
    "cert_kind": ["certification", "licence", "certification"],
    "cert_expiry": ["2029-06-30", "", ""],
}


def _docx(*, main_type: str = WORD_MAIN, extra: dict[str, bytes] | None = None, skip: str = "") -> bytes:
    parts = {
        "[Content_Types].xml": f'<Types><Override PartName="/word/document.xml" ContentType="{main_type}"/></Types>'.encode(),
        "word/document.xml": b"<w:document/>",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            if name != skip:
                archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    get_settings.cache_clear()
    throttle.reset()

    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    # Form tokens have their own tests in test_forms.py, apart from the upload test below.
    app.dependency_overrides[require_csrf] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    throttle.reset()
    get_settings.cache_clear()


async def _register(client: httpx.AsyncClient, email: str = EMAIL) -> None:
    assert (await client.post("/register", data={"email": email, "password": PASSWORD})).status_code == 303


async def _upload(client: httpx.AsyncClient, content: bytes, name: str = "my cv.pdf", kind: str = "application/pdf") -> httpx.Response:
    return await client.post("/profile/cv", files={"cv": (name, content, kind)})


def _applicant(session: Session, email: str = EMAIL) -> Applicant:
    return session.scalar(select(Applicant).where(Applicant.email == email))


def _count(session: Session, model: type, applicant_id: uuid.UUID) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.applicant_id == applicant_id))


# T30: the profile


async def test_t30_the_profile_saves_every_field(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    assert (await client.post("/profile", data=PROFILE)).status_code == 303

    applicant_id = _applicant(session).id
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant_id))
    assert preference.target_roles == ["Backend Developer", "Software Engineer"]
    assert preference.locations == ["Manchester", "Remote"]
    assert preference.salary_amount == 32000
    assert preference.salary_currency == "GBP"
    assert preference.salary_period == "year"
    profile = session.scalar(select(ApplicantProfile).where(ApplicantProfile.applicant_id == applicant_id))
    assert profile.experience_level == ExperienceLevel.entry
    assert profile.travel_distance_miles == 25
    skills = session.scalars(select(ApplicantSkill.text_as_typed).where(ApplicantSkill.applicant_id == applicant_id))
    assert sorted(skills) == ["Python", "SQL"]
    certifications = session.scalars(
        select(ApplicantCertification)
        .where(ApplicantCertification.applicant_id == applicant_id)
        .order_by(ApplicantCertification.name)
    ).all()
    assert [(c.name, c.issuer, c.is_licence, c.expires_on) for c in certifications] == [
        ("AWS Cloud Practitioner", "Amazon", False, date(2029, 6, 30)),
        ("Driving licence", "DVLA", True, None),
    ]


async def test_t30_the_saved_profile_shows_on_the_form(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/profile", data=PROFILE)
    text = (await client.get("/profile")).text
    for expected in ("Backend Developer", "Manchester", 'value="25"', "32000", "Python", "AWS Cloud Practitioner", "DVLA", "2029-06-30"):
        assert expected in text
    assert '<option value="entry" selected>Entry level (graduate or junior)</option>' in text
    assert '<option value="licence" selected>Licence</option>' in text


async def test_t30_saving_again_replaces_the_lists(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await client.post("/profile", data=PROFILE)
    changed = {**PROFILE, "skills": "Django", "cert_name": [""], "cert_issuer": [""], "cert_kind": ["certification"], "cert_expiry": [""]}
    assert (await client.post("/profile", data=changed)).status_code == 303
    applicant_id = _applicant(session).id
    assert list(session.scalars(select(ApplicantSkill.text_as_typed).where(ApplicantSkill.applicant_id == applicant_id))) == ["Django"]
    assert _count(session, ApplicantCertification, applicant_id) == 0
    assert _count(session, ApplicantProfile, applicant_id) == 1
    assert _count(session, ApplicantPreference, applicant_id) == 1


async def test_t30_salary_and_travel_are_optional(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    data = {**PROFILE, "salary_amount": "", "travel_distance": ""}
    assert (await client.post("/profile", data=data)).status_code == 303
    applicant_id = _applicant(session).id
    assert session.scalar(select(ApplicantPreference.salary_amount).where(ApplicantPreference.applicant_id == applicant_id)) is None
    assert session.scalar(select(ApplicantProfile.travel_distance_miles).where(ApplicantProfile.applicant_id == applicant_id)) is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"target_roles": ""}, "target role"),
        ({"locations": " \n "}, "location"),
        ({"experience_level": ""}, "experience level"),
        ({"experience_level": "boss"}, "experience level"),
        ({"travel_distance": "0"}, "Travel distance"),
        ({"travel_distance": "501"}, "Travel distance"),
        ({"travel_distance": "ten"}, "Travel distance"),
        ({"salary_amount": "lots"}, "Salary amount"),
        ({"salary_amount": "-1"}, "salary amount"),
        ({"cert_name": ["", "x"], "cert_issuer": ["Someone", ""]}, "needs a name"),
        ({"cert_expiry": ["soon", "", ""]}, "valid date"),
        ({"target_roles": "\n".join(f"Role {n}" for n in range(11))}, "at most 10"),
        ({"locations": "\n".join(f"Town {n}" for n in range(11))}, "at most 10"),
        ({"skills": "\n".join(f"Skill {n}" for n in range(51))}, "at most 50"),
        ({"target_roles": "x" * 201}, "under 200"),
    ],
)
async def test_t30_a_bad_profile_is_refused_and_keeps_what_was_typed(
    client: httpx.AsyncClient, session: Session, change: dict, message: str
) -> None:
    await _register(client)
    # A refused save rolls back, which must not take the account with it.
    session.commit()
    response = await client.post("/profile", data={**PROFILE, **change})
    assert response.status_code == 400
    assert message in response.text
    assert "AWS Cloud Practitioner" in response.text or "cert_name" in change
    assert _count(session, ApplicantProfile, _applicant(session).id) == 0


async def test_t30_the_profile_needs_a_login_and_touches_only_your_own(client: httpx.AsyncClient, session: Session) -> None:
    assert (await client.get("/profile")).status_code == 303
    assert (await client.post("/profile", data=PROFILE)).status_code == 303
    await _register(client, OTHER_EMAIL)
    await client.post("/logout")
    await _register(client)
    await client.post("/profile", data=PROFILE)
    assert _count(session, ApplicantProfile, _applicant(session, OTHER_EMAIL).id) == 0


async def test_what_a_person_types_on_the_profile_is_escaped(client: httpx.AsyncClient) -> None:
    await _register(client)
    await client.post("/profile", data={**PROFILE, "target_roles": "</textarea><script>x</script>", "skills": "<b>bold</b>"})
    text = (await client.get("/profile")).text
    assert "<script>x" not in text
    assert "<b>bold" not in text


# T31: CV upload


async def test_t31_a_pdf_is_stored_and_only_its_owner_can_download_it(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    assert (await _upload(client, PDF)).status_code == 303

    row = session.scalar(select(CvFile).where(CvFile.applicant_id == _applicant(session).id))
    assert row.file_type == CvFileType.pdf
    assert row.size_bytes == len(PDF)
    assert row.content == PDF

    download = await client.get("/profile/cv")
    assert download.status_code == 200
    assert download.content == PDF
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"] == 'attachment; filename="cv.pdf"'
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"

    client.cookies.clear()
    anonymous = await client.get("/profile/cv")
    assert anonymous.status_code == 303
    assert PDF not in anonymous.content
    await _register(client, OTHER_EMAIL)
    assert (await client.get("/profile/cv")).status_code == 404


async def test_t31_the_file_name_someone_gave_is_never_used(client: httpx.AsyncClient) -> None:
    await _register(client)
    await _upload(client, PDF, name='evil"; x=<script>.pdf')
    assert "evil" not in (await client.get("/profile")).text
    assert "evil" not in (await client.get("/profile/cv")).headers["content-disposition"]


async def test_t31_a_docx_is_accepted(client: httpx.AsyncClient) -> None:
    await _register(client)
    document = _docx()
    assert (await _upload(client, document, "cv.docx", "application/octet-stream")).status_code == 303
    download = await client.get("/profile/cv")
    assert download.content == document
    assert download.headers["content-disposition"] == 'attachment; filename="cv.docx"'
    assert "wordprocessingml" in download.headers["content-type"]


async def test_t31_a_new_upload_replaces_the_old_one(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await _upload(client, PDF)
    document = _docx()
    assert (await _upload(client, document, "cv.docx")).status_code == 303
    assert _count(session, CvFile, _applicant(session).id) == 1
    assert (await client.get("/profile/cv")).content == document


async def test_t31_a_cv_can_be_deleted(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await _upload(client, PDF)
    assert (await client.post("/profile/cv/delete")).status_code == 303
    assert _count(session, CvFile, _applicant(session).id) == 0
    assert (await client.get("/profile/cv")).status_code == 404
    assert (await client.post("/profile/cv/delete")).status_code == 303


async def test_t31_the_profile_page_never_loads_the_file(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await _upload(client, PDF)
    session.expire_all()
    text = (await client.get("/profile")).text
    assert "cv.pdf" in text
    assert "content" not in session.scalar(select(CvFile)).__dict__


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "Choose a PDF"),
        (b"plain text pretending", "Upload a PDF"),
        (b"PK\x03\x04 not really a zip", "Upload a PDF"),
        (b"<html><script>alert(1)</script></html>", "Upload a PDF"),
        (b"MZ\x90\x00 a program", "Upload a PDF"),
        (_docx(skip="word/document.xml"), "Upload a PDF"),
        (_docx(skip="[Content_Types].xml"), "Upload a PDF"),
        (_docx(main_type="application/vnd.oasis.opendocument.text"), "Upload a PDF"),
        (_docx(main_type="application/vnd.ms-word.document.macroEnabled.main+xml"), "macros"),
        (_docx(extra={"word/vbaProject.bin": b"macro code"}), "macros"),
        (_docx(extra={"WORD/VBAPROJECT.BIN": b"macro code"}), "macros"),
    ],
)
def test_t31_files_of_the_wrong_kind_are_refused(content: bytes, message: str) -> None:
    with pytest.raises(CvRefused, match=message):
        check_cv(content)


async def test_t31_a_refused_file_replaces_nothing(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    await _upload(client, PDF)
    session.commit()
    response = await _upload(client, b"plain text", "cv.pdf", "application/pdf")
    assert response.status_code == 400
    assert "Upload a PDF" in response.text
    assert (await client.get("/profile/cv")).content == PDF


def test_t31_the_type_comes_from_the_contents() -> None:
    assert check_cv(PDF) == CvFileType.pdf
    assert check_cv(_docx()) == CvFileType.docx


def test_t31_a_file_of_exactly_5_mb_is_accepted_and_one_byte_more_is_not() -> None:
    limit = PDF + b"x" * (MAX_CV_BYTES - len(PDF))
    assert check_cv(limit) == CvFileType.pdf
    with pytest.raises(CvRefused, match="5 MB"):
        check_cv(limit + b"x")


def test_t31_a_zip_with_too_many_files_or_too_much_unpacked_is_refused() -> None:
    crowded = _docx(extra={f"word/part{n}.xml": b"" for n in range(1001)})
    with pytest.raises(CvRefused):
        check_cv(crowded)
    bomb = _docx(extra={"word/huge.xml": b"\0" * (101 * 1024 * 1024)})
    assert len(bomb) < MAX_CV_BYTES
    with pytest.raises(CvRefused):
        check_cv(bomb)


async def test_t31_an_oversized_upload_is_refused_before_it_is_read(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await _register(client)

    async def _never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the body was read")

    monkeypatch.setattr("starlette.requests.Request.form", _never)
    response = await client.post(
        "/profile/cv",
        content=b"x",
        headers={"content-type": "multipart/form-data; boundary=x", "content-length": str(MAX_UPLOAD_BODY_BYTES + 1)},
    )
    assert response.status_code == 413
    assert "5 MB" in response.text


async def test_t31_an_upload_of_unstated_size_is_refused(client: httpx.AsyncClient) -> None:
    await _register(client)

    async def _chunks() -> AsyncIterator[bytes]:
        yield b"--x\r\n"

    response = await client.post("/profile/cv", content=_chunks(), headers={"content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 411


async def test_t31_uploads_are_limited(client: httpx.AsyncClient) -> None:
    await _register(client)
    for _ in range(20):
        assert (await _upload(client, PDF)).status_code == 303
    assert (await _upload(client, PDF)).status_code == 429


async def test_t31_uploading_needs_a_login(client: httpx.AsyncClient) -> None:
    response = await _upload(client, PDF)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_t31_upload_works_with_the_real_form_token(client: httpx.AsyncClient, session: Session) -> None:
    app.dependency_overrides.pop(require_csrf)
    opened = await client.get("/register")
    token = re.search(r'name="csrf_token" value="([^"]+)"', opened.text).group(1)
    await client.post("/register", data={"email": EMAIL, "password": PASSWORD, "csrf_token": token})
    assert (await _upload(client, PDF)).status_code == 403
    assert _count(session, CvFile, _applicant(session).id) == 0

    token = re.search(r'name="csrf_token" value="([^"]+)"', (await client.get("/profile")).text).group(1)
    response = await client.post("/profile/cv", data={"csrf_token": token}, files={"cv": ("cv.pdf", PDF, "application/pdf")})
    assert response.status_code == 303
    assert _count(session, CvFile, _applicant(session).id) == 1


# T32: account deletion


async def _full_account(client: httpx.AsyncClient, session: Session) -> Applicant:
    await _register(client)
    await client.post("/profile", data=PROFILE)
    await _upload(client, PDF)
    return _applicant(session)


def _search_run(session: Session, applicant_id: uuid.UUID) -> SearchRun:
    session.merge(SearchTemplate(id="T1", search_type="web", provider="serpapi", pattern="p", status=SearchTemplateStatus.active))
    session.flush()
    run = SearchRun(template_id="T1", applicant_id=applicant_id, query="q", provider="serpapi", status=SearchRunStatus.succeeded)
    session.add(run)
    session.flush()
    return run


async def test_t32_deleting_removes_everything_personal_and_keeps_search_history(
    client: httpx.AsyncClient, session: Session
) -> None:
    applicant = await _full_account(client, session)
    applicant_id = applicant.id
    run = _search_run(session, applicant_id)
    run_id = run.id
    models = (ApplicantPreference, ApplicantProfile, ApplicantSkill, ApplicantCertification, CvFile)
    assert all(_count(session, model, applicant_id) >= 1 for model in models)

    response = await client.post("/account/delete", data={"password": PASSWORD})
    assert response.status_code == 200
    assert "has been deleted" in response.text

    session.expire_all()
    assert session.get(Applicant, applicant_id) is None
    assert all(_count(session, model, applicant_id) == 0 for model in (*models, ApplicantJobMatch))
    kept = session.get(SearchRun, run_id)
    assert kept is not None
    assert kept.applicant_id is None


async def test_t32_deleting_ends_the_session_and_the_email_can_register_again(client: httpx.AsyncClient, session: Session) -> None:
    await _full_account(client, session)
    copied = client.cookies["jaa_session"]
    await client.post("/account/delete", data={"password": PASSWORD})
    assert (await client.get("/account")).status_code == 303
    client.cookies.clear()
    client.cookies.set("jaa_session", copied, domain="testserver")
    assert (await client.get("/profile/cv")).status_code == 303
    client.cookies.clear()
    await _register(client)
    assert (await client.get("/profile/cv")).status_code == 404


async def test_t32_deleting_needs_the_right_password(client: httpx.AsyncClient, session: Session) -> None:
    applicant = await _full_account(client, session)
    response = await client.post("/account/delete", data={"password": "not the password"})
    assert response.status_code == 401
    assert session.get(Applicant, applicant.id) is not None
    assert (await client.get("/profile/cv")).content == PDF


async def test_t32_password_guessing_on_delete_is_limited(client: httpx.AsyncClient, session: Session) -> None:
    applicant = await _full_account(client, session)
    for _ in range(5):
        assert (await client.post("/account/delete", data={"password": "not the password"})).status_code == 401
    assert (await client.post("/account/delete", data={"password": PASSWORD})).status_code == 429
    assert session.get(Applicant, applicant.id) is not None


async def test_t32_deleting_leaves_other_people_alone(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client, OTHER_EMAIL)
    await client.post("/profile", data=PROFILE)
    await _upload(client, PDF)
    other_id = _applicant(session, OTHER_EMAIL).id
    await client.post("/logout")
    await _full_account(client, session)
    await client.post("/account/delete", data={"password": PASSWORD})
    session.expire_all()
    assert session.get(Applicant, other_id) is not None
    assert _count(session, CvFile, other_id) == 1
    assert _count(session, ApplicantProfile, other_id) == 1


async def test_t32_deleting_needs_a_login(client: httpx.AsyncClient) -> None:
    assert (await client.post("/account/delete", data={"password": PASSWORD})).status_code == 303


# The Internship level


@pytest.mark.parametrize(
    "title",
    ["Software Engineering Intern", "Backend Developer Internship", "Intern, Backend Engineer", "Backend Engineer Placement"],
)
def test_someone_who_chose_internship_is_matched_to_internships(title: str) -> None:
    found = match_role(title, None, ["Backend Developer", "Software Engineer"], internship=True)
    assert found is not None
    assert found.level == "internship in the title"


@pytest.mark.parametrize(
    "title",
    ["Backend Developer", "Junior Backend Developer", "Senior Software Engineer", "Backend Engineer Apprentice", "Backend Engineering Degree Apprenticeship Placement", "Internal Tools Engineer", "Sales Intern"],
)
def test_someone_who_chose_internship_sees_only_internships_and_no_apprenticeships(title: str) -> None:
    assert match_role(title, None, ["Backend Developer", "Software Engineer"], internship=True) is None


def test_level_words_in_a_target_role_do_not_stop_someone_who_chose_internship() -> None:
    assert match_role("Backend Engineer Intern", None, ["Junior Backend Developer"], internship=True) is not None
    assert match_role("Software Engineer Intern", None, ["Software Engineering Internship"], internship=True) is not None


@pytest.mark.parametrize("title", ["Software Engineering Intern", "Backend Engineer Placement", "Backend Engineer Apprentice"])
def test_everyone_else_still_never_sees_internships_or_apprenticeships(title: str) -> None:
    assert match_role(title, None, ["Backend Developer", "Software Engineer"]) is None
    assert match_role(title, None, ["Junior Backend Developer"]) is None
