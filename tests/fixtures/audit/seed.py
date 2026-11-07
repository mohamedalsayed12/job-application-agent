"""Test-only seeder for classified audit fixtures. Never commit to a shared DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.audit.fixtures import DEFAULT_FIXTURE, load_fixture_companies
from app.audit.types import CLASSIFIED_STATES
from app.models import (
    Candidate,
    CandidateEvidence,
    CandidateState,
    ExtractedBy,
    VerificationCheck,
    VerificationOutcome,
)

# Keep a copy under tests/ for discoverability; canonical JSON is DEFAULT_FIXTURE.
TESTS_COPY = Path(__file__).resolve().parent / "classified_companies.json"


def seed_classified_from_fixture(
    session: Session,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[Candidate]:
    """Insert classified candidates + evidence inside a test transaction."""
    stamp = now or datetime.now(tz=UTC)
    created: list[Candidate] = []
    for offset, raw in enumerate(load_fixture_companies(path or DEFAULT_FIXTURE)):
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
