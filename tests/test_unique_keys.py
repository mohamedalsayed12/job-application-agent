from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.models import (
    EmployerType,
    FetchOutcome,
    FetchRun,
    JobPosting,
    RawPayload,
)

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _constraint_name(exc: IntegrityError) -> str:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if not name:
        raise AssertionError(
            "expected a Postgres unique violation with a constraint name"
        )
    return name


def _declared_unique(model: type, columns: tuple[str, ...]) -> str:
    """Constraints win over indexes. Postgres reports the constraint name."""
    wanted = set(columns)
    table = model.__table__
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            names = {column.name for column in constraint.columns}
            if names == wanted and constraint.name is not None:
                return constraint.name
    for index in table.indexes:
        if index.unique and {column.name for column in index.columns} == wanted:
            if index.name is not None:
                return index.name
    raise AssertionError(f"no unique on {table.name} for {columns}")


def test_d63_saved_levels_survive_the_migration_both_ways(migrated_engine: Engine) -> None:
    config = Config(str(_ALEMBIC_INI))
    with migrated_engine.begin() as conn:
        both = conn.execute(text("INSERT INTO applicant (email) VALUES ('both@example.com') RETURNING id")).scalar()
        conn.execute(
            text("INSERT INTO applicant_profile (applicant_id, experience_levels) VALUES (:id, '{senior,mid}')"),
            {"id": both},
        )
    command.downgrade(config, "0021_interview_job_pack")
    try:
        with migrated_engine.begin() as conn:
            downgraded = conn.execute(
                text("SELECT experience_level FROM applicant_profile WHERE applicant_id = :id"), {"id": both}
            ).scalar()
            single = conn.execute(text("INSERT INTO applicant (email) VALUES ('single@example.com') RETURNING id")).scalar()
            conn.execute(
                text("INSERT INTO applicant_profile (applicant_id, experience_level) VALUES (:id, 'entry')"), {"id": single}
            )
    finally:
        command.upgrade(config, "head")
    with migrated_engine.begin() as conn:
        levels = dict(
            conn.execute(text("SELECT applicant_id, experience_levels FROM applicant_profile WHERE applicant_id IN (:a, :b)"), {"a": both, "b": single}).all()
        )
        conn.execute(text("DELETE FROM applicant WHERE id IN (:a, :b)"), {"a": both, "b": single})
    assert downgraded == "mid"
    assert levels == {both: ["mid"], single: ["entry"]}


def test_models_match_applied_revision(migrated_engine: Engine) -> None:
    assert migrated_engine.dialect.name == "postgresql"
    command.check(Config(str(_ALEMBIC_INI)))
    with migrated_engine.connect() as conn:
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'citext'")
        ).scalar_one_or_none()
    assert ext == "citext"


def test_job_posting_source_and_source_job_id_unique(session: Session) -> None:
    now = datetime.now(UTC)
    fetch = FetchRun(
        source="ats-one",
        outcome=FetchOutcome.complete,
        jobs_returned=1,
        request_count=1,
        started_at=now,
        finished_at=now,
    )
    session.add(fetch)
    session.flush()
    raw = RawPayload(
        fetch_run_id=fetch.id,
        source="ats-one",
        payload={"id": "gh-1"},
        payload_hash="hash-1",
        fetched_at=now,
    )
    session.add(raw)
    session.flush()
    session.add(
        JobPosting(
            source="ats-one",
            source_job_id="gh-1",
            raw_payload_id=raw.id,
            title="Engineer",
            company_name="Acme",
            employer_type=EmployerType.direct,
            salary_known=False,
            apply_url="https://boards.example/gh-1",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    session.flush()
    session.add(
        JobPosting(
            source="ats-one",
            source_job_id="gh-1",
            raw_payload_id=raw.id,
            title="Engineer II",
            company_name="Acme",
            employer_type=EmployerType.direct,
            salary_known=False,
            apply_url="https://boards.example/gh-1",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    with pytest.raises(IntegrityError) as raised:
        session.flush()
    assert _constraint_name(raised.value) == _declared_unique(
        JobPosting, ("source", "source_job_id")
    )
