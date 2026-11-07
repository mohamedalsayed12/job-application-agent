"""Offline fixture helpers for tests and template CSV (never pollute live candidates)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.audit.csv_export import write_audit_csv
from app.audit.types import CLASSIFIED_STATES, AuditSheetRow
from app.models import (
    Candidate,
    CandidateEvidence,
    CandidateState,
    ExtractedBy,
    VerificationCheck,
    VerificationOutcome,
)

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "audit"
    / "classified_companies.json"
)


def load_fixture_companies(path: Path | None = None) -> list[dict[str, Any]]:
    data = json.loads((path or DEFAULT_FIXTURE).read_text(encoding="utf-8"))
    companies = data["companies"]
    if not isinstance(companies, list):
        raise ValueError("fixture companies must be a list")
    return companies


def write_fixture_template_csv(
    out: Path,
    *,
    path: Path | None = None,
    size: int = 20,
) -> tuple[Path, dict[str, int]]:
    """Write an operator template CSV from fixture JSON without touching the DB.

    Ids are fresh UUIDs (not persisted). Use only when live keys are missing;
    replace after a real ``create_audit_sheet`` run.
    """
    companies = load_fixture_companies(path)
    by_state: dict[str, list[dict[str, Any]]] = {state: [] for state in CLASSIFIED_STATES}
    for raw in companies:
        state = str(raw["state"])
        if state not in by_state:
            raise ValueError(f"fixture state not classified: {state}")
        by_state[state].append(raw)

    from itertools import zip_longest

    picked: list[dict[str, Any]] = []
    for group in zip_longest(*(by_state[state] for state in CLASSIFIED_STATES)):
        for raw in group:
            if raw is None:
                continue
            picked.append(raw)
            if len(picked) >= size:
                break
        if len(picked) >= size:
            break
    if len(picked) < size:
        raise ValueError(f"fixture has {len(picked)} rows, need {size}")

    rows: list[AuditSheetRow] = []
    counts: dict[str, int] = {str(state): 0 for state in CLASSIFIED_STATES}
    for raw in picked[:size]:
        state = str(raw["state"])
        counts[state] = counts.get(state, 0) + 1
        urls = tuple(
            str(u)
            for u in (raw.get("evidence_urls") or []) + (raw.get("check_urls") or [])
            if str(u).lower().startswith(("http://", "https://"))
        )
        rows.append(
            AuditSheetRow(
                audit_sample_id=uuid4(),
                candidate_id=uuid4(),
                company_name=str(raw["company_name"]),
                cleaned_name=str(raw["cleaned_name"]),
                primary_domain=raw.get("primary_domain"),
                board_system=raw.get("board_system"),
                board_instance=raw.get("board_instance"),
                board_id=raw.get("board_id"),
                system_result=state,
                evidence_urls=urls,
            )
        )
    return write_audit_csv(out, rows), counts


def seed_classified_from_fixture(
    session: Session,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[Candidate]:
    """Insert classified candidates + evidence for tests only.

    Raises ValueError on unknown states. Does not commit. Do not use this
    against a shared/production database — it invents live-looking candidates.
    """
    stamp = now or datetime.now(tz=UTC)
    created: list[Candidate] = []
    for offset, raw in enumerate(load_fixture_companies(path)):
        state = str(raw["state"])
        if state not in CLASSIFIED_STATES:
            raise ValueError(f"fixture state not classified: {state}")
        created_at = stamp + timedelta(seconds=offset)
        candidate = Candidate(
            company_name=str(raw["company_name"]),
            cleaned_name=str(raw["cleaned_name"]),
            primary_domain=raw.get("primary_domain"),
            board_system=raw.get("board_system"),
            board_instance=raw.get("board_instance"),
            board_id=raw.get("board_id"),
            state=state,
            extracted_by=ExtractedBy.code,
            created_at=created_at,
            updated_at=created_at,
        )
        if state == CandidateState.unsupported:
            candidate.revisit_at = created_at + timedelta(days=90)
        session.add(candidate)
        session.flush()

        for url in raw.get("evidence_urls") or []:
            session.add(
                CandidateEvidence(
                    candidate_id=candidate.id,
                    quoted_text="fixture evidence",
                    source_url=str(url),
                    created_at=created_at,
                )
            )
        for url in raw.get("check_urls") or []:
            session.add(
                VerificationCheck(
                    candidate_id=candidate.id,
                    check_type="fixture",
                    outcome=VerificationOutcome.pass_,
                    evidence_url=str(url),
                    checked_at=created_at,
                )
            )
        created.append(candidate)
    session.flush()
    return created
