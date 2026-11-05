"""Duplicate natural keys raise IntegrityError naming the model constraint."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import UniqueConstraint
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BoardStatus,
    Employer,
    EmployerBoard,
    EmployerType,
    EvidenceLevel,
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


def test_models_match_applied_revision(migrated_engine: Engine) -> None:
    assert migrated_engine.dialect.name == "postgresql"
    command.check(Config(str(_ALEMBIC_INI)))


def test_employer_official_domain_unique(session: Session) -> None:
    session.add(
        Employer(
            name="Acme",
            cleaned_name="acme",
            official_domain="acme.example",
            careers_system="greenhouse",
        )
    )
    session.flush()
    session.add(
        Employer(
            name="Acme Hiring",
            cleaned_name="acme hiring",
            official_domain="acme.example",
            careers_system="lever",
        )
    )
    with pytest.raises(IntegrityError) as raised:
        session.flush()
    assert _constraint_name(raised.value) == _declared_unique(
        Employer, ("official_domain",)
    )


def test_employer_board_system_instance_board_id_unique(session: Session) -> None:
    first = Employer(
        name="Acme",
        cleaned_name="acme",
        official_domain="acme.example",
        careers_system="greenhouse",
    )
    second = Employer(
        name="Other",
        cleaned_name="other",
        official_domain="other.example",
        careers_system="lever",
    )
    session.add_all([first, second])
    session.flush()
    session.add(
        EmployerBoard(
            employer_id=first.id,
            system="greenhouse",
            instance="global",
            board_id="acme",
            evidence_level=EvidenceLevel.strong,
            status=BoardStatus.verified,
        )
    )
    session.flush()
    session.add(
        EmployerBoard(
            employer_id=second.id,
            system="greenhouse",
            instance="global",
            board_id="acme",
            evidence_level=EvidenceLevel.medium,
            status=BoardStatus.verified,
        )
    )
    with pytest.raises(IntegrityError) as raised:
        session.flush()
    assert _constraint_name(raised.value) == _declared_unique(
        EmployerBoard, ("system", "instance", "board_id")
    )


def test_job_posting_source_and_source_job_id_unique(session: Session) -> None:
    now = datetime.now(timezone.utc)
    fetch = FetchRun(
        source="greenhouse",
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
        source="greenhouse",
        payload={"id": "gh-1"},
        payload_hash="hash-1",
        fetched_at=now,
    )
    session.add(raw)
    session.flush()
    session.add(
        JobPosting(
            source="greenhouse",
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
            source="greenhouse",
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
