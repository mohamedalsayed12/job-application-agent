"""D43: reading a CV with AI to fill the profile form for the person to check."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.api.jobs import get_gateway
from app.api.profile import ProfileFields, fill_from_cv
from app.applicants.cv_read import (
    Certification,
    CvSuggestions,
    CvUnreadable,
    cv_text,
    grounded,
)
from app.applicants.resume_parse import restore_skills
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    ApplicantPreference,
    BudgetPolicy,
    CvFile,
    CvFileType,
    SpendKind,
    SpendLedger,
)
from app.skills.taxonomy import load_taxonomy
from tests.jobs import _public_resolve
from tests.test_profile import PASSWORD, PROFILE, WORD_MAIN

CV_LINES = [
    "Sam Example",
    "Manchester, United Kingdom",
    "BSc Business Management, University of Manchester, 2027",
    "Skills: Python, FastAPI, PostgreSQL & SQL",
    "Junior Developer, Acme Ltd, Jan 2025 - Present",
    "Cached search results in Redis and built the React front end",
    "Set up GitHub Actions to run the tests on every push",
    "Projects: built a job search API in Python",
    "Full UK driving licence",
]
# Resume-Matcher's parse of CV_LINES, with made-up items (Leeds, Kubernetes, AWS) and without the
# skills named only in the job bullets (Redis, React, GitHub Actions).
FOUND = {
    "personalInfo": {
        "name": "Sam Example", "title": "", "email": "", "phone": "", "location": "Manchester, United Kingdom",
        "website": "", "linkedin": "", "github": "",
    },
    "summary": "",
    "workExperience": [
        {
            "id": 1, "title": "Junior Developer", "company": "Acme Ltd", "location": "", "years": "Jan 2025 - Present",
            "description": [CV_LINES[5], CV_LINES[6]],
        }
    ],
    "education": [
        {"id": 1, "institution": "University of Manchester", "degree": "BSc Business Management", "years": "2027", "description": ""}
    ],
    "personalProjects": [],
    "additional": {
        "technicalSkills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes"],
        "languages": [],
        "certificationsTraining": [
            {"name": "Full UK driving licence", "issuer": "DVLA", "is_licence": True, "expires_on": ""},
            {"name": "AWS Solutions Architect", "issuer": "Amazon", "is_licence": False, "expires_on": "2030-01-01"},
        ],
        "awards": [],
    },
    "jobSearch": {
        "targetRoles": ["Junior Python Developer", "Graduate Software Engineer"],
        "locations": ["Manchester", "Leeds"],
        "aimedAt": "",
        "experienceLevels": ["entry"],
    },
}
FLAT = {
    "target_roles": ["Junior Python Developer", "Graduate Software Engineer"],
    "locations": ["Manchester", "Leeds"],
    "experience_levels": ["entry"],
    "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes"],
    "certifications": FOUND["additional"]["certificationsTraining"],
}


def _parsed(*, roles: list[str] | None = None, aimed_at: str = "", **additional: object) -> dict:
    """FOUND with its job-search block and additional fields changed."""
    return {
        **FOUND,
        "jobSearch": {**FOUND["jobSearch"], "aimedAt": aimed_at, **({"targetRoles": roles} if roles else {})},
        "additional": {**FOUND["additional"], **additional},
    }


def _docx(lines: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{line.replace("&", "&amp;")}</w:t><w:tab/></w:r></w:p>' for line in lines
    )
    parts = {
        "[Content_Types].xml": f'<Types><Override PartName="/word/document.xml" ContentType="{WORD_MAIN}"/></Types>',
        "word/document.xml": f"<w:document><w:body>{body}</w:body></w:document>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _pdf(lines: list[str]) -> bytes:
    text = " ".join(
        f"({line.replace('(', '[').replace(')', ']')}) Tj 0 -16 Td" for line in lines
    )
    stream = f"BT /F1 11 Tf 72 760 Td {text} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_d43_text_comes_out_of_a_word_cv_one_paragraph_per_line() -> None:
    text = cv_text(_docx(CV_LINES), CvFileType.docx, max_chars=10_000)
    assert text.splitlines() == CV_LINES


def test_d43_text_comes_out_of_a_pdf_cv() -> None:
    text = cv_text(_pdf(CV_LINES), CvFileType.pdf, max_chars=10_000)
    for line in ("Sam Example", "Skills: Python, FastAPI", "Full UK driving licence"):
        assert line in text


@pytest.mark.parametrize(
    ("content", "file_type"),
    [(b"%PDF-1.7\nmade up contents of a CV", CvFileType.pdf), (_docx(["Hi"]), CvFileType.docx)],
)
def test_d43_a_file_without_readable_text_is_refused(content: bytes, file_type: CvFileType) -> None:
    with pytest.raises(CvUnreadable):
        cv_text(content, file_type, max_chars=10_000)


def test_d43_the_job_a_cv_is_written_for_must_appear_in_it() -> None:
    text = "\n".join(["Data Analyst", *CV_LINES])
    kept = grounded({**FLAT, "aimed_at": "Data Analyst"}, text)
    invented = grounded({**FLAT, "aimed_at": "Astronaut"}, text)
    assert kept.aimed_at == "Data Analyst"
    assert invented.aimed_at == ""
    inside = grounded({**FLAT, "aimed_at": "IT"}, "Worked with ward teams")
    own_word = grounded({**FLAT, "aimed_at": "IT"}, "IT Support\nWorked with ward teams")
    assert inside.aimed_at == ""
    assert own_word.aimed_at == "IT"


def test_d43_a_typed_location_and_level_stay() -> None:
    have = _fields(locations="Leeds", experience_levels=["mid"])
    found = CvSuggestions(locations=["Manchester"], experience_levels=["senior"])
    merged, filled, _ = fill_from_cv(have, found)
    assert merged.locations == "Leeds"
    assert merged.experience_levels == ["mid"]
    assert filled == []


def test_d43_only_what_the_cv_says_is_kept_but_roles_may_be_inferred() -> None:
    found = grounded({**FLAT, "experience_levels": ["expert", "entry", "mid"]}, "\n".join(CV_LINES))
    assert found.target_roles == ["Junior Python Developer", "Graduate Software Engineer"]
    assert found.locations == ["Manchester"]
    assert found.skills == ["Python", "FastAPI", "PostgreSQL"]
    assert found.certifications == [Certification("Full UK driving licence", "", True, "")]
    assert found.experience_levels == ["entry", "mid"]


def test_d45_degrees_read_from_a_cv_must_be_in_it() -> None:
    cv = "Sam Example\nBSc (Hons) Business Management, University of Manchester, 2027"
    found = grounded({"degrees": ["BSc Business Management", "MSc Computer Science"]}, cv)
    assert found.degrees == ["BSc Business Management"]


def _offered(text: str, *roles: str) -> bool:
    return all(f'name="cv_roles" value="{role}"/>' in text for role in roles)


def test_d43_a_skill_or_place_must_be_a_whole_word_in_the_cv() -> None:
    text = "Sam, Manchester. Built apps in JavaScript at Google. Excellence awards 2025."
    made_up = {
        "skills": ["Java", "Go", "R", "C", "Excel", "Script"],
        "locations": ["Man", "Chester"],
        "certifications": [{"name": "CE", "issuer": "Go", "is_licence": False, "expires_on": ""}],
    }
    found = grounded(made_up, text)
    assert (found.skills, found.locations, found.certifications) == ([], [], [])


def test_d43_whole_words_still_match_symbols_and_phrases() -> None:
    text = "Skills: C++, C#, .NET, Node.js and Go.\nMachine learning in Python/Django. AWS Certified Cloud Practitioner (Amazon)."
    payload = {
        "skills": ["C++", "C#", ".NET", "Node.js", "Go", "Machine Learning", "Python", "Django", "C", "JS", "Node"],
        "certifications": [{"name": "AWS Certified Cloud Practitioner", "issuer": "Amazon", "is_licence": False, "expires_on": ""}],
    }
    found = grounded(payload, text)
    assert found.skills == ["C++", "C#", ".NET", "Node.js", "Go", "Machine Learning", "Python", "Django"]
    assert found.certifications == [Certification("AWS Certified Cloud Practitioner", "Amazon", False, "")]


def _fields(**values: object) -> ProfileFields:
    blank = dict.fromkeys(("target_roles", "locations", "salary_amount", "travel_distance", "skills", "degrees"), "")
    lists = dict.fromkeys(("experience_levels", "cert_name", "cert_issuer", "cert_kind", "cert_expiry", "cv_roles", "cv_offer"), [])
    return ProfileFields(**{**blank, "salary_currency": "GBP", "salary_period": "year", **lists, **values})  # type: ignore[arg-type]


def test_d43_what_the_person_already_has_always_stays() -> None:
    have = _fields(target_roles="Data Analyst", skills="python\nExcel", cert_name=["Full UK driving licence"], cert_issuer=["DVLA"], cert_kind=["licence"], cert_expiry=[""])
    found = CvSuggestions(
        target_roles=["Junior Python Developer"],
        locations=["Manchester"],
        experience_levels=["entry"],
        skills=["Python", "FastAPI"],
        certifications=[Certification("Full UK driving licence", "", True, ""), Certification("ITIL 4", "", False, "")],
    )
    merged, filled, offer = fill_from_cv(have, found)
    assert merged.target_roles == "Data Analyst"
    assert offer == ["Junior Python Developer"]
    assert merged.locations == "Manchester"
    assert merged.experience_levels == ["entry"]
    assert merged.skills.splitlines() == ["python", "Excel", "FastAPI"]
    assert [row[0] for row in merged.certifications] == ["Full UK driving licence", "ITIL 4"]
    assert filled == ["locations", "experience levels", "1 skill", "1 certification or licence"]


class _Claude:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload, self.calls, self.sent, self.titles_asked, self.broken = payload or FOUND, 0, "", 0, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][0]["content"]
        if "<TITLE_DATA>" in content:
            self.titles_asked += 1
            answer = {"titles": ["Backend Developer", "Senior Python Developer"]}
        else:
            self.calls += 1
            if self.broken:
                return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
            self.sent, answer = content, self.payload
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 900, "output_tokens": 200},
            },
        )

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"api.anthropic.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )


@pytest.fixture
def claude() -> _Claude:
    return _Claude()


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


def _money(session: Session, cap: str) -> None:
    session.add(
        BudgetPolicy(
            provider="*", period="month", money_cap=Decimal(cap), request_cap=None, currency="GBP",
            effective_from=datetime.now(UTC) - timedelta(days=1), effective_to=None,
        )
    )
    session.flush()


DOCX = ("cv.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
PDF = ("cv.pdf", "application/pdf")


async def _register_and_upload(
    client: httpx.AsyncClient, kind: tuple[str, str] = DOCX, lines: list[str] = CV_LINES
) -> httpx.Response:
    assert (await client.post("/register", data={"email": "cv@example.com", "password": PASSWORD})).status_code == 303
    content = _docx(lines) if kind == DOCX else _pdf(lines)
    return await client.post("/profile/cv", files={"cv": (kind[0], content, kind[1])})


@pytest.mark.parametrize("kind", [DOCX, PDF], ids=["docx", "pdf"])
async def test_d43_uploading_a_cv_fills_the_form_but_saves_nothing(
    client: httpx.AsyncClient, session: Session, claude: _Claude, kind: tuple[str, str]
) -> None:
    load_taxonomy(session)
    _money(session, "2")
    response = await _register_and_upload(client, kind)

    assert response.status_code == 200
    assert "We read your CV and filled in locations, experience levels, " in response.text
    assert "1 certification or licence, 1 degree" in response.text
    assert "Tick any job titles from your CV you want to search for." in response.text
    assert "The file is saved" in response.text
    assert 'placeholder="Junior Software Engineer"></textarea>' in response.text
    for role in FLAT["target_roles"]:
        assert f'name="cv_roles" value="{role}"/> {role}</label>' in response.text
    assert ">Manchester</textarea>" in response.text
    skills = response.text.split('name="skills"')[1].split(">", 1)[1].split("</textarea>")[0].splitlines()
    assert skills[:3] == ["Python", "FastAPI", "PostgreSQL"]
    assert {"Redis", "React", "GitHub Actions"} <= set(skills)
    for made_up in ("Kubernetes", "Leeds", "AWS Solutions Architect"):
        assert made_up not in response.text
    assert "Full UK driving licence" in response.text
    assert "BSc Business Management</textarea>" in response.text
    assert 'name="experience_levels" value="entry" checked/>' in response.text
    assert "<CV_DATA>" in claude.sent and "BSc Business Management" in claude.sent
    assert session.scalar(select(ApplicantPreference)) is None
    spend = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_cv_read)).all()
    assert len(spend) == 1


async def test_d43_suggestions_stay_after_refresh_until_save(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "2")
    await _register_and_upload(client)

    again = await client.get("/profile")
    assert again.status_code == 200
    assert _offered(again.text, *FLAT["target_roles"])
    assert "The file is saved" in again.text
    assert 'name="experience_levels" value="entry" checked/>' in again.text
    assert "cv.docx" in again.text
    assert session.scalar(select(ApplicantPreference)) is None

    claude.broken = True
    failed = await client.post("/profile/cv/fill")
    assert "Try again later" in failed.text
    assert _offered(failed.text, *FLAT["target_roles"])
    assert _offered((await client.get("/profile")).text, *FLAT["target_roles"])

    rejected = await client.post(
        "/profile",
        data={
            "target_roles": "Edited Role Only",
            "locations": "",
            "experience_levels": "entry",
            "skills": "Python",
            "salary_currency": "GBP",
            "salary_period": "year",
        },
    )
    assert rejected.status_code == 400
    assert "Edited Role Only" in rejected.text
    assert "Edited Role Only" in (await client.get("/profile")).text
    assert "Junior Python Developer" not in (await client.get("/profile")).text

    saved = await client.post(
        "/profile",
        data={
            "target_roles": "Junior Python Developer\nGraduate Software Engineer",
            "locations": "Manchester",
            "experience_levels": "entry",
            "skills": "Python\nFastAPI\nPostgreSQL",
            "salary_currency": "GBP",
            "salary_period": "year",
        },
    )
    assert saved.status_code == 303
    assert session.scalar(select(ApplicantPreference)) is not None
    after_save = await client.get("/profile")
    assert "Junior Python Developer\nGraduate Software Engineer</textarea>" in after_save.text
    assert "The file is saved" not in after_save.text


async def test_d43_a_second_cv_adds_to_the_draft_and_deleting_its_source_drops_it(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "2")
    await _register_and_upload(client)
    first = session.scalars(select(CvFile)).one()

    claude.payload = _parsed(roles=["Data Analyst"], technicalSkills=["Python"], certificationsTraining=[])
    files = {"cv": ("two.docx", _docx(CV_LINES), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    second_page = await client.post("/profile/cv", files=files)
    assert _offered(second_page.text, "Junior Python Developer", "Graduate Software Engineer", "Data Analyst")
    second = session.scalars(select(CvFile).where(CvFile.id != first.id)).one()

    await client.post(f"/profile/cv/{first.id}/delete", data={})
    assert _offered((await client.get("/profile")).text, "Data Analyst", "Junior Python Developer")

    await client.post(f"/profile/cv/{second.id}/delete", data={})
    assert "Junior Python Developer" not in (await client.get("/profile")).text


def test_d43_draft_cookie_budget_counts_unicode_escapes() -> None:
    from app.api.profile import _MAX_SET_COOKIE_BYTES, _PROFILE_DRAFT, _set_cookie_bytes

    fields = _fields(target_roles="软" * 500, locations="Manchester", skills="Python", experience_levels=["entry"])
    payload = {"notice": "filled", **fields.to_draft()}
    base = {
        "applicant": "00000000-0000-0000-0000-000000000001",
        "version": 1,
        "csrf": "c" * 43,
    }
    assert len(json.dumps(payload, ensure_ascii=False)) < 2500
    assert _set_cookie_bytes({**base, _PROFILE_DRAFT: payload}) > _MAX_SET_COOKIE_BYTES


@pytest.mark.parametrize("kind", [DOCX, PDF], ids=["docx", "pdf"])
async def test_d43_a_scanned_or_blank_cv_is_not_sent(
    client: httpx.AsyncClient, session: Session, claude: _Claude, kind: tuple[str, str]
) -> None:
    _money(session, "2")
    response = await _register_and_upload(client, kind, lines=[" "])
    assert "find text in your CV. A scanned PDF has none." in response.text
    assert claude.calls == 0


def test_d43_skills_the_parse_dropped_from_a_skills_line_come_back() -> None:
    text = "Skills: Python, FastAPI, Pydantic, Alembic\nBuilt services with Python, FastAPI and Celery for a charity"
    parsed = restore_skills({**FOUND, "additional": {**FOUND["additional"], "technicalSkills": ["Python", "FastAPI"]}}, text)
    assert parsed["additional"]["technicalSkills"] == ["Python", "FastAPI", "Pydantic", "Alembic"]


async def test_d43_the_fill_button_reads_the_stored_cv_again(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    _money(session, "2")
    await _register_and_upload(client)
    again = await client.post("/profile/cv/fill")
    assert again.status_code == 200
    assert "We read your CV but found nothing new" in again.text
    assert _offered(again.text, *FLAT["target_roles"])
    assert claude.calls == 2


async def test_d43_with_no_money_left_the_cv_is_not_sent(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    _money(session, "0")
    response = await _register_and_upload(client)
    assert "AI budget is used up" in response.text
    assert claude.calls == 0


async def test_cv_titles_are_offered_even_when_the_person_has_typed_some(client: httpx.AsyncClient, session: Session) -> None:
    _money(session, "2")
    await _register_and_upload(client)
    assert (await client.post("/profile", data={**PROFILE, "target_roles": "graduate software engineer"})).status_code == 303

    again = await client.post("/profile/cv/fill")

    assert 'name="cv_roles" value="Junior Python Developer"/>' in again.text
    assert 'value="Graduate Software Engineer"' not in again.text


async def test_ticked_cv_titles_are_added_to_the_target_roles_once(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "2")
    await _register_and_upload(client)
    form = {**PROFILE, "target_roles": "graduate software engineer", "cv_roles": ["Junior Python Developer", "Graduate Software Engineer"]}

    assert (await client.post("/profile", data=form)).status_code == 303

    saved = session.scalar(select(ApplicantPreference))
    assert saved.target_roles == ["graduate software engineer", "Junior Python Developer"]
    page = await client.get("/profile")
    assert "graduate software engineer\nJunior Python Developer</textarea>" in page.text
    assert "We also search: Backend Developer." in page.text
    assert claude.titles_asked == 2

    assert (await client.post("/profile", data=form)).status_code == 303
    assert claude.titles_asked == 2


async def test_ticked_cv_titles_alone_are_enough_to_save(client: httpx.AsyncClient, session: Session) -> None:
    _money(session, "2")
    await _register_and_upload(client)

    assert (await client.post("/profile", data={**PROFILE, "target_roles": "", "cv_roles": ["Junior Python Developer"]})).status_code == 303

    assert session.scalar(select(ApplicantPreference)).target_roles == ["Junior Python Developer"]


async def test_d43_without_a_cv_the_fill_button_goes_back_to_the_profile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": "cv@example.com", "password": PASSWORD})).status_code == 303
    response = await client.post("/profile/cv/fill")
    assert (response.status_code, response.headers["location"]) == (303, "/profile")


async def test_d43_typed_answers_stay_and_an_empty_label_takes_the_title_in_the_cv(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    from tests.test_profile import PROFILE

    _money(session, "2")
    assert (await client.post("/register", data={"email": "cv@example.com", "password": PASSWORD})).status_code == 303
    saved = await client.post("/profile", data={**PROFILE, "target_roles": "Data Analyst", "skills": "Excel"})
    assert saved.status_code == 303
    claude.payload = _parsed(aimed_at="Astronaut")
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    first = await client.post(
        "/profile/cv",
        data={"label": "Nurse"},
        files={"cv": ("cv.docx", _docx(CV_LINES), mime)},
    )
    assert first.status_code == 200
    assert 'value="Nurse"' in first.text
    assert 'value="Astronaut"' not in first.text
    assert ">Data Analyst</textarea>" in first.text
    assert _offered(first.text, *FLAT["target_roles"])
    skills = first.text.split('name="skills"')[1].split(">", 1)[1].split("</textarea>")[0].splitlines()
    assert skills[:4] == ["Excel", "Python", "FastAPI", "PostgreSQL"]
    assert session.scalar(select(ApplicantPreference)).target_roles == ["Data Analyst"]

    claude.payload = _parsed(aimed_at="Data Analyst")
    second = await client.post(
        "/profile/cv",
        files={"cv": ("cv.docx", _docx(["Data Analyst", *CV_LINES]), mime)},
    )
    assert second.status_code == 200
    assert 'value="Nurse"' in second.text
    assert 'value="Data Analyst"' in second.text
