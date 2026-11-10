"""T13 + T16 retry path: candidate queue dedupe and verification backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BoardStatus,
    Candidate,
    CandidateEvidence,
    CandidateState,
    Employer,
    EmployerBoard,
    EvidenceLevel,
    ExtractedBy,
)
from app.queue import (
    VERIFICATION_RETRY_DELAYS,
    claim_next_candidate,
    ingest_candidate,
    next_retry_schedule,
    release_stale_claims,
    schedule_verification_failure,
)
from app.queue.names import clean_company_name


def _evidence(n: int = 1) -> list[tuple[None, str, str]]:
    return [(None, f"evidence quote {n}"[:200], f"https://example.test/{n}")]


def test_t13_three_searches_one_candidate_by_board(session: Session) -> None:
    """Same board found three times → one queued candidate; verification claims once."""
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    results = [
        ingest_candidate(
            session,
            company_name="Example Corp Ltd",
            extracted_by=ExtractedBy.code,
            board_system="greenhouse",
            board_instance="greenhouse",
            board_id="example",
            evidence=_evidence(i),
            now=now,
        )
        for i in range(1, 4)
    ]
    session.flush()

    ids = {r.survivor.id for r in results}
    assert len(ids) == 1
    survivor = results[0].survivor
    assert survivor.state == CandidateState.queued
    assert survivor.cleaned_name == clean_company_name("Example Corp Ltd")
    assert all(not r.created for r in results[1:])

    evidence_count = session.scalar(
        select(func.count())
        .select_from(CandidateEvidence)
        .where(CandidateEvidence.candidate_id == survivor.id)
    )
    assert evidence_count == 3

    live = session.scalars(
        select(Candidate).where(Candidate.state == CandidateState.queued)
    ).all()
    assert len(live) == 1

    survivor.domain_searched_at = now  # R2: CF3 has run for this board
    claimed = claim_next_candidate(session, now=now)
    assert claimed is not None
    assert claimed.id == survivor.id
    assert claimed.state == CandidateState.verifying
    assert claimed.attempt_count == 1

    assert claim_next_candidate(session, now=now) is None


def test_t13_name_alone_never_merges_across_domains(session: Session) -> None:
    first = ingest_candidate(
        session,
        company_name="Acme Ltd",
        extracted_by=ExtractedBy.code,
        primary_domain="acme.example",
        evidence=_evidence(1),
    )
    second = ingest_candidate(
        session,
        company_name="Acme Limited",
        extracted_by=ExtractedBy.code,
        primary_domain="acme-other.example",
        evidence=_evidence(2),
    )
    session.flush()

    assert first.survivor.cleaned_name == second.survivor.cleaned_name == "acme"
    assert first.survivor.id != second.survivor.id
    assert first.survivor.state == CandidateState.queued
    assert second.survivor.state == CandidateState.queued


def test_t13_name_with_disjoint_domain_candidates_stay_separate(session: Session) -> None:
    first = ingest_candidate(
        session,
        company_name="Acme Ltd",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["acme.com", "acme.co.uk"],
        evidence=_evidence(1),
    )
    second = ingest_candidate(
        session,
        company_name="Acme Limited",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["other-acme.com", "other-acme.io"],
        evidence=_evidence(2),
    )
    session.flush()
    assert first.survivor.id != second.survivor.id
    assert first.survivor.primary_domain is None
    assert second.survivor.primary_domain is None


def test_t13_name_without_domain_groups_into_one(session: Session) -> None:
    rows = [
        ingest_candidate(
            session,
            company_name="Brightly Software",
            extracted_by=ExtractedBy.ai,
            evidence=_evidence(i),
        )
        for i in range(1, 4)
    ]
    session.flush()
    assert len({r.survivor.id for r in rows}) == 1
    assert rows[0].survivor.state == CandidateState.queued
    assert rows[0].reason == "fresh"
    assert rows[1].reason == "name_group"


def test_dedupe_board_already_in_directory(session: Session) -> None:
    employer = Employer(
        name="Example",
        cleaned_name="example",
        official_domain="example.example",
        careers_system="greenhouse",
    )
    session.add(employer)
    session.flush()
    session.add(
        EmployerBoard(
            employer_id=employer.id,
            system="greenhouse",
            instance="greenhouse",
            board_id="example",
            evidence_level=EvidenceLevel.strong,
            status=BoardStatus.verified,
        )
    )
    session.flush()

    first = ingest_candidate(
        session,
        company_name="Example",
        extracted_by=ExtractedBy.code,
        board_system="greenhouse",
        board_instance="greenhouse",
        board_id="example",
        evidence=_evidence(1),
    )
    second = ingest_candidate(
        session,
        company_name="Example Corp",
        extracted_by=ExtractedBy.code,
        board_system="greenhouse",
        board_instance="greenhouse",
        board_id="example",
        evidence=_evidence(2),
    )
    session.flush()
    assert first.reason == "board_directory"
    assert first.survivor.state == CandidateState.duplicate
    assert first.survivor.employer_id == employer.id
    assert second.survivor.id == first.survivor.id
    evidence_count = session.scalar(
        select(func.count())
        .select_from(CandidateEvidence)
        .where(CandidateEvidence.candidate_id == first.survivor.id)
    )
    assert evidence_count == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.board_id == "example")
        )
        == 1
    )


def test_import_app_queue_has_no_cycle() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    proc = subprocess.run(
        [sys.executable, "-c", "from app.queue import ingest_candidate, claim_next_candidate"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr


def test_dedupe_discovery_cache_blocks_before_revisit(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    cached = Candidate(
        company_name="Workday Co",
        cleaned_name="workday co",
        primary_domain="workdayco.example",
        state=CandidateState.unsupported,
        extracted_by=ExtractedBy.code,
        revisit_at=now + timedelta(days=90),
    )
    session.add(cached)
    session.flush()

    result = ingest_candidate(
        session,
        company_name="Workday Co",
        extracted_by=ExtractedBy.code,
        primary_domain="workdayco.example",
        evidence=_evidence(),
        now=now,
    )
    session.flush()
    assert result.reason == "discovery_cache"
    assert result.survivor.id == cached.id
    assert result.survivor.state == CandidateState.unsupported
    queued = session.scalars(
        select(Candidate).where(Candidate.state == CandidateState.queued)
    ).all()
    assert queued == []


def test_t16_retry_path_timeouts_then_needs_review(session: Session) -> None:
    """Timeouts schedule 1h / 6h / 24h then needs_review — never unsupported/rejected."""
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    ingest_candidate(
        session,
        company_name="Flaky Board Ltd",
        extracted_by=ExtractedBy.code,
        board_system="lever",
        board_instance="lever_global",
        board_id="flaky",
        evidence=_evidence(),
        now=now,
    ).survivor.domain_searched_at = now  # R2: CF3 has run
    session.flush()

    delays = []
    for step, expected_delay in enumerate(VERIFICATION_RETRY_DELAYS):
        claimed = claim_next_candidate(session, now=now)
        assert claimed is not None
        assert claimed.state == CandidateState.verifying
        assert claimed.attempt_count == step + 1

        plan = schedule_verification_failure(
            claimed,
            error="timeout",
            now=now,
        )
        session.flush()
        assert plan.state == CandidateState.retry_wait
        assert plan.delay == expected_delay
        assert claimed.state == CandidateState.retry_wait
        assert claimed.next_attempt_at == now + expected_delay
        assert claimed.state not in (
            CandidateState.unsupported,
            CandidateState.rejected,
        )
        delays.append(expected_delay)
        now = claimed.next_attempt_at

    claimed = claim_next_candidate(session, now=now)
    assert claimed is not None
    assert claimed.attempt_count == 4
    plan = schedule_verification_failure(claimed, error="timeout", now=now)
    session.flush()
    assert plan.state == CandidateState.needs_review
    assert claimed.state == CandidateState.needs_review
    assert claimed.next_attempt_at is None
    assert claimed.last_error == "timeout"
    assert claimed.state not in (CandidateState.unsupported, CandidateState.rejected)

    assert delays == [
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
    ]


def test_next_retry_schedule_units() -> None:
    assert next_retry_schedule(1).delay == timedelta(hours=1)
    assert next_retry_schedule(2).delay == timedelta(hours=6)
    assert next_retry_schedule(3).delay == timedelta(hours=24)
    assert next_retry_schedule(4).state == CandidateState.needs_review
    with pytest.raises(ValueError):
        next_retry_schedule(0)


def test_stale_verifying_claim_recovered(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    row = Candidate(
        company_name="Stuck",
        cleaned_name="stuck",
        state=CandidateState.verifying,
        extracted_by=ExtractedBy.code,
        attempt_count=1,
        claimed_at=now - timedelta(minutes=20),
    )
    session.add(row)
    session.flush()

    n = release_stale_claims(session, now=now)
    session.flush()
    assert n == 1
    assert row.state == CandidateState.retry_wait
    assert row.attempt_count == 1
    assert row.claimed_at is None
    assert row.next_attempt_at == now


def test_name_only_unsupported_stays_cached(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    cached = Candidate(
        company_name="Brightly Software",
        cleaned_name="brightly software",
        state=CandidateState.unsupported,
        extracted_by=ExtractedBy.ai,
        revisit_at=now + timedelta(days=90),
        domain_candidates=[],
    )
    session.add(cached)
    session.flush()
    result = ingest_candidate(
        session,
        company_name="Brightly Software",
        extracted_by=ExtractedBy.ai,
        evidence=_evidence(),
        now=now,
    )
    session.flush()
    assert result.survivor.id == cached.id
    assert result.reason == "discovery_cache"
    assert result.survivor.state == CandidateState.unsupported


def test_due_unsupported_is_requeued_not_duplicated(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    cached = Candidate(
        company_name="Workday Co",
        cleaned_name="workday co",
        primary_domain="workdayco.example",
        state=CandidateState.unsupported,
        extracted_by=ExtractedBy.code,
        revisit_at=now - timedelta(seconds=1),
        attempt_count=3,
    )
    session.add(cached)
    session.flush()
    result = ingest_candidate(
        session,
        company_name="Workday Co",
        extracted_by=ExtractedBy.code,
        primary_domain="workdayco.example",
        evidence=_evidence(),
        now=now,
    )
    session.flush()
    assert result.survivor.id == cached.id
    assert result.survivor.state == CandidateState.queued
    assert result.survivor.attempt_count == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.primary_domain == "workdayco.example")
        )
        == 1
    )


def test_multi_domain_candidates_merge_on_shared_host(session: Session) -> None:
    first = ingest_candidate(
        session,
        company_name="Acme",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["https://www.acme.com/jobs", "acme.com"],
        evidence=_evidence(1),
    )
    second = ingest_candidate(
        session,
        company_name="Acme Inc",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["http://acme.com"],
        evidence=_evidence(2),
    )
    session.flush()
    assert first.survivor.id == second.survivor.id
    assert first.survivor.primary_domain == "acme.com"


def test_directory_hit_marks_queued_duplicate(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    queued = ingest_candidate(
        session,
        company_name="Example",
        extracted_by=ExtractedBy.code,
        board_system="greenhouse",
        board_instance="greenhouse",
        board_id="example",
        evidence=_evidence(1),
        now=now,
    ).survivor
    assert queued.state == CandidateState.queued
    employer = Employer(
        name="Example",
        cleaned_name="example",
        official_domain="example.example",
        careers_system="greenhouse",
    )
    session.add(employer)
    session.flush()
    session.add(
        EmployerBoard(
            employer_id=employer.id,
            system="greenhouse",
            instance="greenhouse",
            board_id="example",
            evidence_level=EvidenceLevel.strong,
            status=BoardStatus.verified,
        )
    )
    session.flush()
    result = ingest_candidate(
        session,
        company_name="Example",
        extracted_by=ExtractedBy.code,
        board_system="greenhouse",
        board_instance="greenhouse",
        board_id="example",
        evidence=_evidence(2),
        now=now,
    )
    session.flush()
    assert result.survivor.id == queued.id
    assert result.survivor.state == CandidateState.duplicate
    assert claim_next_candidate(session, now=now) is None


def test_unsupported_revisit_requeues(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    row = Candidate(
        company_name="Old Unsupported",
        cleaned_name="old unsupported",
        primary_domain="oldunsupported.test",  # unsupported implies a confirmed domain
        state=CandidateState.unsupported,
        extracted_by=ExtractedBy.code,
        revisit_at=now - timedelta(seconds=1),
        attempt_count=3,
    )
    session.add(row)
    session.flush()

    claimed = claim_next_candidate(session, now=now)
    session.flush()
    assert claimed is not None
    assert claimed.id == row.id
    assert claimed.state == CandidateState.verifying
    assert claimed.attempt_count == 1
