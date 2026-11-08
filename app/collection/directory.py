"""Stage 7: move verified candidates into the directory every applicant shares."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BoardStatus,
    Candidate,
    CandidateState,
    Employer,
    EmployerBoard,
    EvidenceLevel,
    ReviewDecision,
    ReviewItem,
)

REVERIFY_AFTER = timedelta(days=30)


@dataclass(slots=True)
class RegistrationReport:
    registered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def register_verified_boards(session: Session, *, now: datetime | None = None) -> RegistrationReport:
    """Safe to repeat: only candidates still in ``verified`` are picked up."""
    stamp = now or datetime.now(UTC)
    report = RegistrationReport()
    candidates = session.scalars(
        select(Candidate).where(Candidate.state == CandidateState.verified).order_by(Candidate.company_name)
    ).all()
    for candidate in candidates:
        if not (
            candidate.primary_domain
            and candidate.board_system
            and candidate.board_instance
            and candidate.board_id
        ):
            report.skipped.append(candidate.company_name)
            continue
        employer = _employer_for(session, candidate)
        _board_for(session, candidate, employer, stamp)
        candidate.employer_id = employer.id
        candidate.state = CandidateState.registered
        report.registered.append(candidate.company_name)
    session.flush()
    return report


def _employer_for(session: Session, candidate: Candidate) -> Employer:
    employer = session.scalar(select(Employer).where(Employer.official_domain == candidate.primary_domain))
    if employer is None:
        employer = Employer(
            name=candidate.company_name,
            cleaned_name=candidate.cleaned_name,
            official_domain=candidate.primary_domain,
            careers_system=candidate.board_system,
        )
        session.add(employer)
        session.flush()
    return employer


def _board_for(session: Session, candidate: Candidate, employer: Employer, now: datetime) -> EmployerBoard:
    board = session.scalar(
        select(EmployerBoard).where(
            EmployerBoard.system == candidate.board_system,
            EmployerBoard.instance == candidate.board_instance,
            EmployerBoard.board_id == candidate.board_id,
        )
    )
    if board is None:
        board = EmployerBoard(
            employer_id=employer.id,
            system=candidate.board_system,
            instance=candidate.board_instance,
            board_id=candidate.board_id,
            evidence_level=_evidence_level(session, candidate),
            status=BoardStatus.verified,
            verified_at=now,
            next_reverify_at=now + REVERIFY_AFTER,
        )
        session.add(board)
        session.flush()
    return board


def _evidence_level(session: Session, candidate: Candidate) -> str:
    """The verifier only passes strong evidence. A reviewer can approve on less."""
    approved = session.scalar(
        select(ReviewItem.id).where(
            ReviewItem.candidate_id == candidate.id,
            ReviewItem.decision == ReviewDecision.verified,
        )
    )
    return EvidenceLevel.medium if approved else EvidenceLevel.strong
