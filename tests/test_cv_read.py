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
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    ApplicantPreference,
    BudgetPolicy,
    CvFileType,
    SpendKind,
    SpendLedger,
)
from tests.test_ai_extraction import _public_resolve
from tests.test_profile import PASSWORD, WORD_MAIN

CV_LINES = [
    "Sam Example",
    "Manchester, United Kingdom",
    "BSc Business Management, University of Manchester, 2027",
    "Skills: Python, FastAPI, PostgreSQL & SQL",
    "Projects: built a job search API in Python",
    "Full UK driving licence",
]
FOUND = {
    "target_roles": ["Junior Python Developer", "Graduate Software Engineer"],
    "locations": ["Manchester", "Leeds"],
    "experience_level": "entry",
    "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes"],
    "certifications": [
        {"name": "Full UK driving licence", "issuer": "DVLA", "is_licence": True, "expires_on": ""},
        {"name": "AWS Solutions Architect", "issuer": "Amazon", "is_licence": False, "expires_on": "2030-01-01"},
    ],
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


def test_d43_only_what_the_cv_says_is_kept_but_roles_may_be_inferred() -> None:
    found = grounded({**FOUND, "experience_level": "expert"}, "\n".join(CV_LINES))
    assert found.target_roles == ["Junior Python Developer", "Graduate Software Engineer"]
    assert found.locations == ["Manchester"]
    assert found.skills == ["Python", "FastAPI", "PostgreSQL"]
    assert found.certifications == [Certification("Full UK driving licence", "", True, "")]
    assert found.experience_level == ""


def _fields(**values: object) -> ProfileFields:
    blank = dict.fromkeys(("target_roles", "locations", "salary_amount", "experience_level", "travel_distance", "skills", "degrees"), "")
    lists = dict.fromkeys(("cert_name", "cert_issuer", "cert_kind", "cert_expiry"), [])
    return ProfileFields(**{**blank, "salary_currency": "GBP", "salary_period": "year", **lists, **values})  # type: ignore[arg-type]


def test_d43_what_the_person_already_has_always_stays() -> None:
    have = _fields(target_roles="Data Analyst", skills="python\nExcel", cert_name=["Full UK driving licence"], cert_issuer=["DVLA"], cert_kind=["licence"], cert_expiry=[""])
    found = CvSuggestions(
        target_roles=["Junior Python Developer"],
        locations=["Manchester"],
        experience_level="entry",
        skills=["Python", "FastAPI"],
        certifications=[Certification("Full UK driving licence", "", True, ""), Certification("ITIL 4", "", False, "")],
    )
    merged, filled = fill_from_cv(have, found)
    assert merged.target_roles == "Data Analyst"
    assert merged.locations == "Manchester"
    assert merged.experience_level == "entry"
    assert merged.skills.splitlines() == ["python", "Excel", "FastAPI"]
    assert [row[0] for row in merged.certifications] == ["Full UK driving licence", "ITIL 4"]
    assert filled == ["locations", "experience level", "1 skill", "1 certification or licence"]


class _Claude:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload, self.calls, self.sent = payload or FOUND, 0, ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.sent = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps(self.payload)}],
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


async def _register_and_upload(client: httpx.AsyncClient) -> httpx.Response:
    assert (await client.post("/register", data={"email": "cv@example.com", "password": PASSWORD})).status_code == 303
    files = {"cv": ("cv.docx", _docx(CV_LINES), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    return await client.post("/profile/cv", files=files)


async def test_d43_uploading_a_cv_fills_the_form_but_saves_nothing(
    client: httpx.AsyncClient, session: Session, claude: _Claude
) -> None:
    _money(session, "2")
    response = await _register_and_upload(client)

    assert response.status_code == 200
    assert "We read your CV and filled in target roles, locations, experience level, 3 skills, 1 certification or licence" in response.text
    assert "Junior Python Developer\nGraduate Software Engineer</textarea>" in response.text
    assert "Python\nFastAPI\nPostgreSQL</textarea>" in response.text
    assert "Kubernetes" not in response.text
    assert '<option value="entry" selected>' in response.text
    assert "<CV_DATA>" in claude.sent and "BSc Business Management" in claude.sent
    assert session.scalar(select(ApplicantPreference)) is None
    spend = session.scalars(select(SpendLedger).where(SpendLedger.kind == SpendKind.ai_cv_read)).all()
    assert len(spend) == 1


async def test_d43_the_fill_button_reads_the_stored_cv_again(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    _money(session, "2")
    await _register_and_upload(client)
    again = await client.post("/profile/cv/fill")
    assert again.status_code == 200
    assert "We read your CV and filled in" in again.text
    assert claude.calls == 2


async def test_d43_with_no_money_left_the_cv_is_not_sent(client: httpx.AsyncClient, session: Session, claude: _Claude) -> None:
    _money(session, "0")
    response = await _register_and_upload(client)
    assert "AI budget is used up" in response.text
    assert claude.calls == 0


async def test_d43_without_a_cv_the_fill_button_goes_back_to_the_profile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/register", data={"email": "cv@example.com", "password": PASSWORD})).status_code == 303
    response = await client.post("/profile/cv/fill")
    assert (response.status_code, response.headers["location"]) == (303, "/profile")
