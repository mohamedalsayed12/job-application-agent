"""Several CVs per person (D61)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounts import throttle
from app.api.forms import require_csrf
from app.applicants.cv import (
    MAX_CVS,
    CvRefused,
    base_cv,
    delete_cv,
    pick_base_cv,
    save_cv_choices,
    store_cv,
)
from app.applicants.service import create_applicant, parse_preference_input
from app.applications.extract import ExtractResult
from app.applications.prepare import prepare_application
from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    Applicant,
    ApplicantPreference,
    Application,
    ApplicationStatus,
    CvFile,
    TailoredCv,
    TailoredCvStatus,
)
from app.tailor.run import package_hash
from tests.jobs import _job, _li, _public_resolve, _store
from tests.test_profile import (
    OTHER_EMAIL,
    PDF,
    PROFILE,
    _applicant,
    _docx,
    _register,
    _upload,
)

PDF_A = b"%PDF-1.7\nanalyst"
PDF_B = b"%PDF-1.7\nbackend"


def _choice(label: str, primary: bool, when: datetime) -> SimpleNamespace:
    return SimpleNamespace(label=label, is_primary=primary, uploaded_at=when)


def test_two_jobs_get_different_cvs() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    analyst = _choice("Data Analyst", False, older)
    backend = _choice("Backend Engineer", True, newer)
    cvs = [analyst, backend]
    assert pick_base_cv(cvs, title="Junior Data Analyst", advert="Prepare weekly sales reports") is analyst
    assert pick_base_cv(cvs, title="Backend Engineer", advert="Build HTTP services") is backend
    assert pick_base_cv(cvs) is backend


def test_business_development_does_not_beat_the_primary_for_a_backend_engineer_job() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    primary = _choice("Product Manager", True, older)
    business = _choice("Business Development", False, newer)
    developer = _choice("Backend Developer", False, newer)
    assert pick_base_cv([business, primary], title="Backend Engineer", advert="Build HTTP services") is primary
    assert pick_base_cv([primary, developer], title="Backend Engineer", advert="Build HTTP services") is developer


def test_a_different_cv_changes_the_tailored_package_hash() -> None:
    posting = "Backend Engineer. Build HTTP services."
    assert package_hash(posting=posting, cv_bytes=PDF_A) != package_hash(posting=posting, cv_bytes=PDF_B)


def _person(session: Session) -> Applicant:
    return create_applicant(
        session,
        email=f"{uuid.uuid4()}@example.com",
        preference=parse_preference_input(target_roles=["Data Analyst"], locations=["Manchester"]),
    )


def test_the_eleventh_cv_is_refused_and_only_one_can_be_primary(session: Session) -> None:
    person = _person(session)
    first = store_cv(session, person.id, PDF_A, label="Data Analyst")
    second = store_cv(session, person.id, PDF_B, label="Backend Engineer")
    assert first.is_primary is True
    assert second.is_primary is False
    for n in range(MAX_CVS - 2):
        store_cv(session, person.id, PDF + bytes([n]))
    with pytest.raises(CvRefused, match="10"):
        store_cv(session, person.id, PDF + b"x")

    save_cv_choices(session, person.id, {second.id: "Platform Engineer"}, second.id)
    session.expire_all()
    rows = list(session.scalars(select(CvFile).where(CvFile.applicant_id == person.id)))
    primary = [row for row in rows if row.is_primary]
    assert len(primary) == 1
    assert primary[0].id == second.id
    assert primary[0].label == "Platform Engineer"

    second.is_primary = True
    first.is_primary = True
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_a_cv_clears_it_on_the_package_and_the_application(session: Session) -> None:
    person = _person(session)
    cv = store_cv(session, person.id, PDF_A, label="Data Analyst")
    other = store_cv(session, person.id, PDF_B, label="Backend Engineer")
    _store(session, [_li(1, title="Data Analyst", content="Prepare weekly sales reports in Excel.")])
    job = _job(session, 1)
    application = Application(
        applicant_id=person.id,
        job_id=job.id,
        apply_url=job.apply_url,
        status=ApplicationStatus.ready_to_fill.value,
        cv_file_id=cv.id,
    )
    package = TailoredCv(
        applicant_id=person.id,
        job_id=job.id,
        status=TailoredCvStatus.ready.value,
        input_hash="h",
        prompt_version="test",
        cv_file_id=cv.id,
    )
    session.add_all([application, package])
    session.flush()

    delete_cv(session, person.id, cv.id)
    session.expire_all()
    assert session.get(Application, application.id).cv_file_id is None
    assert session.get(TailoredCv, package.id).cv_file_id is None
    left = session.scalar(select(CvFile).where(CvFile.applicant_id == person.id))
    assert left.id == other.id
    assert left.is_primary is True


def test_the_loader_picks_the_cv_whose_label_fits_the_job(session: Session) -> None:
    person = _person(session)
    analyst = store_cv(session, person.id, PDF_A, label="Data Analyst")
    backend = store_cv(session, person.id, PDF_B, label="Backend Engineer")
    _store(
        session,
        [
            _li(1, title="Data Analyst", content="Prepare weekly sales reports in Excel."),
            _li(2, title="Backend Engineer", content="Build HTTP services for the payments platform."),
        ],
    )
    assert base_cv(session, person.id, _job(session, 1)).id == analyst.id
    assert base_cv(session, person.id, _job(session, 2)).id == backend.id


async def test_preparing_an_application_records_the_cv_for_that_job(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_form(*_args: object, **_kwargs: object) -> ExtractResult:
        return ExtractResult(None, error="no form")

    monkeypatch.setattr("app.applications.prepare.extract_form_schema", _no_form)
    person = _person(session)
    analyst = store_cv(session, person.id, PDF_A, label="Data Analyst")
    store_cv(session, person.id, PDF_B, label="Backend Engineer")
    _store(session, [_li(1, title="Data Analyst", content="Prepare weekly sales reports in Excel.")])
    job = _job(session, 1)
    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset(),
        resolve=_public_resolve,
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    row = await prepare_application(
        session,
        applicant=person,
        job_id=job.id,
        gateway=gateway,
        settings=get_settings(),
        persist=session.flush,
    )
    assert row.cv_file_id == analyst.id
    assert row.status == ApplicationStatus.failed.value


@pytest.fixture
async def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
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


async def test_the_profile_lists_each_cv_and_another_person_cannot_download_it(
    client: httpx.AsyncClient, session: Session
) -> None:
    await _register(client)
    await _upload(client, PDF)
    await _upload(client, _docx(), "cv.docx")
    page = (await client.get("/profile")).text
    assert "Upload another CV" in page
    assert "Primary" in page
    ids = re.findall(r"/profile/cv/([0-9a-f-]{36})", page)
    assert len(set(ids)) == 2
    download = await client.get(f"/profile/cv/{ids[0]}")
    assert download.status_code == 200
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert "attachment" in download.headers["content-disposition"]

    owner = _applicant(session)
    rows = {str(row.id): row for row in session.scalars(select(CvFile).where(CvFile.applicant_id == owner.id))}
    second = next(row for row in rows.values() if not row.is_primary)
    chosen = await client.post(
        "/profile",
        data={
            **PROFILE,
            "cv_id": list(rows),
            "cv_label": ["Data Analyst" if row.is_primary else "Backend Engineer" for row in rows.values()],
            "cv_primary": str(second.id),
        },
    )
    assert chosen.status_code == 303
    session.expire_all()
    assert session.get(CvFile, second.id).is_primary is True
    assert (await client.get("/profile/cv")).content == second.content

    client.cookies.clear()
    await _register(client, OTHER_EMAIL)
    stranger = await client.get(f"/profile/cv/{ids[0]}")
    assert stranger.status_code == 404
    assert PDF not in stranger.content
    missing = await client.get(f"/profile/cv/{uuid.uuid4()}")
    assert missing.status_code == 404


async def test_a_non_numeric_cv_id_does_not_save(client: httpx.AsyncClient, session: Session) -> None:
    await _register(client)
    assert (await client.post("/profile", data=PROFILE)).status_code == 303
    response = await client.post(
        "/profile",
        data={**PROFILE, "target_roles": "Data Analyst", "cv_id": ["not-a-number"], "cv_label": ["Business Development"]},
    )
    assert response.status_code == 400
    session.expire_all()
    person = _applicant(session)
    preference = session.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == person.id))
    assert preference is not None
    assert preference.target_roles == ["Backend Developer", "Software Engineer"]
