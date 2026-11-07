"""Step 10 part 1: stratified audit sampling + operator CSV (no judgments)."""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.csv_export import csv_text, write_audit_csv
from app.audit.fixtures import write_fixture_template_csv
from app.audit.sample import (
    InsufficientClassifiedError,
    create_audit_sheet,
    list_classified_candidates,
    stratified_sample,
)
from tests.fixtures.audit.seed import seed_classified_from_fixture
from app.audit.types import CLASSIFIED_STATES, CSV_COLUMNS, DEFAULT_SAMPLE_SIZE
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.models import AuditSample, Candidate, CandidateState, ExtractedBy

ADMIN = "test-admin-key-step10"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(session: Session, admin_env: None) -> AsyncIterator[httpx.AsyncClient]:
    def _override() -> Session:
        return session

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def _bare_candidate(
    session: Session,
    *,
    name: str,
    state: str,
    domain: str | None = None,
) -> Candidate:
    row = Candidate(
        company_name=name,
        cleaned_name=name.lower(),
        primary_domain=domain,
        state=state,
        extracted_by=ExtractedBy.code,
        created_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_fixture_seed_has_at_least_20_classified(session: Session) -> None:
    seeded = seed_classified_from_fixture(session)
    assert len(seeded) >= DEFAULT_SAMPLE_SIZE
    states = {c.state for c in seeded}
    assert set(states) == set(CLASSIFIED_STATES)


def test_stratified_sample_covers_all_three_states(session: Session) -> None:
    seed_classified_from_fixture(session)
    classified = list_classified_candidates(session)
    picked = stratified_sample(classified, size=20)
    assert len(picked) == 20
    by_state = {state: 0 for state in CLASSIFIED_STATES}
    for row in picked:
        by_state[row.state] += 1
    assert by_state[CandidateState.verified] >= 1
    assert by_state[CandidateState.needs_review] >= 1
    assert by_state[CandidateState.unsupported] >= 1
    assert max(by_state.values()) - min(by_state.values()) <= 1


def test_stratified_sample_insufficient() -> None:
    with pytest.raises(InsufficientClassifiedError):
        stratified_sample([], size=20)


def test_create_audit_sheet_leaves_auditor_fields_null(
    session: Session, tmp_path: Path
) -> None:
    seed_classified_from_fixture(session)
    rows, summary = create_audit_sheet(session, size=20)
    assert summary.selected == 20
    assert sum(summary.by_state.values()) == 20

    samples = list(session.scalars(select(AuditSample)))
    assert len(samples) == 20
    for sample in samples:
        assert sample.system_result in CLASSIFIED_STATES
        assert sample.auditor_result is None
        assert sample.is_correct is None
        assert sample.is_useful is None
        assert sample.notes is None
        assert sample.candidate_id is not None

    path = write_audit_csv(tmp_path / "audit.csv", rows)
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    assert reader.fieldnames == list(CSV_COLUMNS)
    csv_rows = list(reader)
    assert len(csv_rows) == 20
    for row in csv_rows:
        assert row["system_result"] in CLASSIFIED_STATES
        assert row["auditor_result"] == ""
        assert row["is_correct"] == ""
        assert row["is_useful"] == ""
        assert row["notes"] == ""
        assert row["evidence_urls"]


def test_second_call_reexports_open_sheet(session: Session) -> None:
    seed_classified_from_fixture(session)
    first, _ = create_audit_sheet(session, size=20)
    second, _ = create_audit_sheet(session, size=20)
    assert len(first) == 20
    assert [row.audit_sample_id for row in second] == [
        row.audit_sample_id for row in first
    ]
    assert session.scalar(select(func.count()).select_from(AuditSample)) == 20


def test_fixture_template_csv_has_no_db_side_effects(
    session: Session, tmp_path: Path
) -> None:
    path, counts = write_fixture_template_csv(tmp_path / "template.csv", size=20)
    assert path.exists()
    assert sum(counts.values()) == 20
    assert session.scalar(select(func.count()).select_from(Candidate)) == 0
    assert session.scalar(select(func.count()).select_from(AuditSample)) == 0


def test_csv_text_matches_columns(session: Session) -> None:
    seed_classified_from_fixture(session)
    rows, _ = create_audit_sheet(session, size=3)
    body = csv_text(rows)
    assert body.splitlines()[0] == ",".join(CSV_COLUMNS)


@pytest.mark.asyncio
async def test_admin_audit_requires_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/audit")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_audit_sample_download(
    client: httpx.AsyncClient, session: Session
) -> None:
    seed_classified_from_fixture(session)
    session.flush()
    response = await client.post(
        f"/admin/audit/sample?admin_key={ADMIN}",
        data={"size": "20"},
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "step10-audit-sample.csv" in response.headers.get("content-disposition", "")
    assert "selected=20" in response.headers.get("x-audit-summary", "")
    reader = csv.DictReader(StringIO(response.text))
    assert reader.fieldnames == list(CSV_COLUMNS)
    assert len(list(reader)) == 20
    count = session.scalar(select(func.count()).select_from(AuditSample))
    assert count == 20


@pytest.mark.asyncio
async def test_admin_audit_insufficient_redirect(
    client: httpx.AsyncClient, session: Session
) -> None:
    _bare_candidate(
        session, name="Only One", state=CandidateState.verified, domain="only.test"
    )
    response = await client.post(
        f"/admin/audit/sample?admin_key={ADMIN}",
        data={"size": "20"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
