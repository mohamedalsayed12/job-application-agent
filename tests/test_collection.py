"""Registration and daily collection: T4, T22 and T27."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.sample import (
    create_audit_sheet,
    list_classified_candidates,
    stratified_sample,
)
from app.collection import collect_due, directory, is_due, register_verified_boards
from app.fetch.gateway import FetchGateway
from app.matching.entry_level import SearchReport, load_board_jobs
from app.models import (
    Applicant,
    BudgetPolicy,
    Candidate,
    CandidateState,
    Employer,
    EmployerBoard,
    EvidenceLevel,
    ExtractedBy,
    FetchOutcome,
    FetchRun,
    RawPayload,
    ReviewDecision,
    ReviewItem,
    SpendLedger,
)
from app.worker import run_cycle

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
GREENHOUSE_BODY = {"jobs": [{"id": 11, "title": "Backend Developer"}, {"id": 12, "title": "Data Engineer"}]}


def _candidate(
    session: Session,
    name: str = "Acme",
    *,
    state: CandidateState = CandidateState.verified,
    board_id: str | None = "acme",
    domain: str | None = "acme.example",
) -> Candidate:
    candidate = Candidate(
        company_name=name,
        cleaned_name=name.lower(),
        primary_domain=domain,
        board_system="greenhouse" if board_id else None,
        board_instance="greenhouse" if board_id else None,
        board_id=board_id,
        state=state,
        extracted_by=ExtractedBy.code,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _board(session: Session, *, system: str = "greenhouse", last_checked: datetime | None = None) -> EmployerBoard:
    employer = Employer(name="Acme", cleaned_name="acme", official_domain="acme.example", careers_system=system)
    session.add(employer)
    session.flush()
    board = EmployerBoard(
        employer_id=employer.id,
        system=system,
        instance="greenhouse" if system == "greenhouse" else "lever_global",
        board_id="acme",
        evidence_level=EvidenceLevel.strong,
        status="verified",
        last_checked_at=last_checked,
        last_job_count=7,
    )
    session.add(board)
    session.flush()
    return board


def _gateway(handler: Callable[[httpx.Request], httpx.Response]) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.anthropic.com"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),
    )


def _answer(status: int, body: bytes | dict | list, requests: list[str] | None = None) -> Callable:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(str(request.url))
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    return handler


def test_verified_candidate_joins_the_directory(session: Session) -> None:
    candidate = _candidate(session)
    report = register_verified_boards(session, now=NOW)
    board = session.scalar(select(EmployerBoard))
    assert report.registered == ["Acme"]
    assert candidate.state == CandidateState.registered
    assert candidate.employer_id == board.employer_id
    assert (board.system, board.instance, board.board_id) == ("greenhouse", "greenhouse", "acme")
    assert board.evidence_level == EvidenceLevel.strong
    assert board.next_reverify_at == NOW + timedelta(days=30)


def test_registering_twice_changes_nothing(session: Session) -> None:
    _candidate(session)
    register_verified_boards(session, now=NOW)
    assert register_verified_boards(session, now=NOW).registered == []
    assert len(session.scalars(select(EmployerBoard)).all()) == 1
    assert len(session.scalars(select(Employer)).all()) == 1


def test_reviewer_approved_board_is_not_recorded_as_strong_evidence(session: Session) -> None:
    candidate = _candidate(session)
    session.add(ReviewItem(candidate_id=candidate.id, reason="ownership_medium", priority=30, decision=ReviewDecision.verified))
    session.flush()
    register_verified_boards(session, now=NOW)
    assert session.scalar(select(EmployerBoard)).evidence_level == EvidenceLevel.medium


@pytest.mark.parametrize("state", [CandidateState.needs_review, CandidateState.unsupported, CandidateState.queued])
def test_only_verified_candidates_are_registered(session: Session, state: CandidateState) -> None:
    _candidate(session, state=state)
    assert register_verified_boards(session, now=NOW).registered == []
    assert session.scalar(select(EmployerBoard)) is None


def test_verified_candidate_without_a_board_is_reported_and_left_alone(session: Session) -> None:
    candidate = _candidate(session, board_id=None)
    assert register_verified_boards(session, now=NOW).skipped == ["Acme"]
    assert candidate.state == CandidateState.verified


async def test_registered_boards_stay_in_the_job_search(session: Session) -> None:
    _candidate(session, state=CandidateState.registered)
    gateway = _gateway(_answer(200, {"jobs": [{"id": 1, "title": "Python Developer", "absolute_url": "https://x.example/1"}]}))
    jobs = await load_board_jobs(session, gateway, include_review=False, report=SearchReport())
    await gateway.aclose()
    assert [job.title for job in jobs] == ["Python Developer"]


def test_audit_grades_a_registered_board_as_verified(session: Session) -> None:
    registered = _candidate(session, state=CandidateState.registered)
    assert list_classified_candidates(session) == [registered]
    assert stratified_sample([registered], size=1) == [registered]
    rows, summary = create_audit_sheet(session, size=1)
    assert [row.system_result for row in rows] == [CandidateState.verified]
    assert summary.classified_available[CandidateState.verified] == 1


async def test_complete_fetch_stores_the_run_and_every_raw_job(session: Session) -> None:
    board = _board(session)
    gateway = _gateway(_answer(200, GREENHOUSE_BODY))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    run = session.scalar(select(FetchRun))
    raw = session.scalars(select(RawPayload).order_by(RawPayload.source_job_id)).all()
    assert report.outcomes == {FetchOutcome.complete: 1}
    assert (run.outcome, run.source, run.employer_board_id, run.jobs_returned) == (FetchOutcome.complete, "greenhouse", board.id, 2)
    assert [r.source_job_id for r in raw] == ["11", "12"]
    assert raw[0].payload["title"] == "Backend Developer"
    assert all(r.fetch_run_id == run.id and r.payload_hash for r in raw)
    assert (board.last_checked_at, board.last_job_count, board.consecutive_failures) == (NOW, 2, 0)


async def test_greenhouse_is_read_with_full_adverts(session: Session) -> None:
    _board(session)
    requests: list[str] = []
    gateway = _gateway(_answer(200, GREENHOUSE_BODY, requests))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert requests == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]


async def test_lever_board_is_collected(session: Session) -> None:
    board = _board(session, system="lever")
    gateway = _gateway(_answer(200, [{"id": "abc", "text": "Backend Developer"}]))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert session.scalar(select(RawPayload)).source_job_id == "abc"
    assert board.last_job_count == 1


@pytest.mark.parametrize(
    "response",
    [
        (500, {"error": "server"}),
        (404, {"error": "gone"}),
        (200, b""),
        (200, b'{"jobs": [{"id": 1'),
        (200, {"unexpected": True}),
    ],
    ids=["server_error", "not_found", "empty_body", "half_finished", "unexpected_shape"],
)
async def test_t22_a_bad_read_never_looks_like_a_complete_one(session: Session, response: tuple) -> None:
    board = _board(session)
    gateway = _gateway(_answer(*response))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    run = session.scalar(select(FetchRun))
    assert run.outcome not in (FetchOutcome.complete, FetchOutcome.empty)
    assert board.last_job_count == 7
    assert board.consecutive_failures == 1
    assert session.scalar(select(RawPayload)) is None
    assert len(report.failed_boards) == 1


async def test_t22_a_timeout_is_recorded_as_failed(session: Session) -> None:
    board = _board(session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    gateway = _gateway(handler)
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert session.scalar(select(FetchRun)).outcome == FetchOutcome.failed
    assert board.last_job_count == 7


async def test_empty_board_is_a_complete_read_of_zero_jobs(session: Session) -> None:
    board = _board(session)
    gateway = _gateway(_answer(200, {"jobs": []}))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert session.scalar(select(FetchRun)).outcome == FetchOutcome.empty
    assert (board.last_job_count, board.consecutive_failures) == (0, 0)


async def test_advert_with_a_nul_character_is_stored(session: Session) -> None:
    _board(session)
    gateway = _gateway(_answer(200, {"jobs": [{"id": 1, "title": "Back\u0000end"}]}))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert session.scalar(select(RawPayload)).payload["title"] == "Backend"


async def test_advert_with_a_literal_backslash_u0000_is_kept_as_written(session: Session) -> None:
    _board(session)
    gateway = _gateway(_answer(200, {"jobs": [{"id": 1, "title": "C:\\u0000"}]}))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert session.scalar(select(RawPayload)).payload["title"] == "C:\\u0000"


async def test_advert_the_database_refuses_is_a_failed_read_and_the_next_board_still_runs(session: Session) -> None:
    bad = _board(session)
    employer = Employer(name="Good", cleaned_name="good", official_domain="good.example", careers_system="greenhouse")
    session.add(employer)
    session.flush()
    good = EmployerBoard(
        employer_id=employer.id,
        system="greenhouse",
        instance="greenhouse",
        board_id="good",
        evidence_level=EvidenceLevel.strong,
        status="verified",
    )
    session.add(good)
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        title = "\\ud800" if "/acme/" in str(request.url) else "Backend Developer"
        body = '{"jobs": [{"id": 1, "title": "' + title + '"}]}'
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})

    gateway = _gateway(handler)
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert report.outcomes == {FetchOutcome.failed: 1, FetchOutcome.complete: 1}
    assert report.failed_boards == ["greenhouse/acme: failed (storing jobs: DataError)"]
    assert (bad.last_job_count, bad.consecutive_failures) == (7, 1)
    assert good.last_job_count == 1
    assert len(session.scalars(select(RawPayload)).all()) == 1


async def test_advert_too_deep_to_process_is_a_failed_read_not_a_crash(session: Session) -> None:
    board = _board(session)
    deep = "[" * 3000 + "]" * 3000
    body = '{"jobs": [{"id": 1, "title": ' + deep + "}]}"
    gateway = _gateway(_answer(200, body.encode()))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert report.outcomes == {FetchOutcome.failed: 1}
    assert board.consecutive_failures == 1
    assert session.scalar(select(RawPayload)) is None


async def test_lever_board_is_read_only_on_its_own_host(session: Session) -> None:
    board = _board(session, system="lever")
    board.instance = "lever_eu"
    session.flush()
    requests: list[str] = []
    gateway = _gateway(_answer(200, [{"id": "abc", "text": "Backend Developer"}], requests))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert requests == ["https://api.eu.lever.co/v0/postings/acme?mode=json"]


async def test_lever_board_gone_from_its_host_does_not_fall_back_to_the_other(session: Session) -> None:
    board = _board(session, system="lever")
    requests: list[str] = []
    gateway = _gateway(_answer(404, {"error": "gone"}, requests))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert requests == ["https://api.lever.co/v0/postings/acme?mode=json"]
    assert board.consecutive_failures == 1


async def test_unsupported_system_is_refused_and_not_read_as_lever(session: Session) -> None:
    board = _board(session, system="ashby")
    requests: list[str] = []
    gateway = _gateway(_answer(200, [{"id": "abc"}], requests))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert requests == []
    assert session.scalar(select(RawPayload)) is None
    assert board.consecutive_failures == 1
    assert "unsupported board: ashby" in report.failed_boards[0]


def test_one_company_failing_to_register_does_not_block_the_others(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _candidate(session, "Bad", board_id="bad", domain="bad.example")
    good = _candidate(session, "Good", board_id="good", domain="good.example")
    real = directory._employer_for

    def employer_for(session_: Session, candidate: Candidate) -> Employer:
        if candidate.company_name == "Bad":
            raise IntegrityError("insert", {}, Exception("taken"))
        return real(session_, candidate)

    monkeypatch.setattr(directory, "_employer_for", employer_for)
    report = register_verified_boards(session, now=NOW)
    assert report.registered == ["Good"]
    assert report.skipped == ["Bad: IntegrityError"]
    assert bad.state == CandidateState.verified
    assert good.state == CandidateState.registered


def test_audit_summary_counts_registered_as_verified(session: Session) -> None:
    _candidate(session, state=CandidateState.registered)
    _, summary = create_audit_sheet(session, size=1)
    assert summary.by_state == {CandidateState.verified: 1}


async def test_unexpected_fetch_error_is_a_failed_read_and_the_next_board_still_runs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _board(session)
    employer = Employer(name="Good", cleaned_name="good", official_domain="good.example", careers_system="lever")
    session.add(employer)
    session.flush()
    good = EmployerBoard(
        employer_id=employer.id,
        system="lever",
        instance="lever_global",
        board_id="good",
        evidence_level=EvidenceLevel.strong,
        status="verified",
    )
    session.add(good)
    session.flush()

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug in a board client")

    monkeypatch.setattr("app.collection.collect.fetch_greenhouse_board", broken)
    gateway = _gateway(_answer(200, [{"id": "abc", "text": "Backend Developer"}]))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert report.outcomes == {FetchOutcome.failed: 1, FetchOutcome.complete: 1}
    assert (bad.last_job_count, bad.consecutive_failures) == (7, 1)
    assert good.last_job_count == 1


async def test_t4_collection_continues_when_the_budget_is_used_up(session: Session) -> None:
    session.add(
        BudgetPolicy(
            provider="*", period="month", money_cap=Decimal("0"), request_cap=None, currency="GBP",
            effective_from=datetime(2020, 1, 1, tzinfo=UTC), effective_to=None,
        )
    )
    _board(session)
    gateway = _gateway(_answer(200, GREENHOUSE_BODY))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert report.jobs == 2
    assert session.scalar(select(SpendLedger)) is None


async def test_t27_a_board_is_read_once_however_many_applicants_want_it(session: Session) -> None:
    session.add_all([Applicant(email=f"applicant{i}@example.test") for i in range(3)])
    _board(session)
    requests: list[str] = []
    gateway = _gateway(_answer(200, GREENHOUSE_BODY, requests))
    await collect_due(session, gateway, clock=lambda: NOW)
    second = await collect_due(session, gateway, clock=lambda: NOW + timedelta(hours=1))
    await gateway.aclose()
    assert len(requests) == 1
    assert not second.outcomes


async def test_board_is_collected_again_the_next_day(session: Session) -> None:
    _board(session, last_checked=NOW - timedelta(days=1))
    gateway = _gateway(_answer(200, GREENHOUSE_BODY))
    report = await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert report.jobs == 2


@pytest.mark.parametrize(
    ("failures", "since_last", "expected"),
    [
        (0, timedelta(hours=23), False),
        (0, timedelta(hours=24), True),
        (1, timedelta(seconds=59), False),
        (1, timedelta(minutes=1), True),
        (2, timedelta(minutes=5), True),
        (3, timedelta(minutes=29), False),
        (3, timedelta(minutes=30), True),
        (4, timedelta(hours=23), False),
        (4, timedelta(hours=24), True),
    ],
)
def test_failed_reads_retry_soon_then_fall_back_to_daily(
    failures: int, since_last: timedelta, expected: bool
) -> None:
    board = EmployerBoard(last_checked_at=NOW - since_last, consecutive_failures=failures)
    assert is_due(board, NOW) is expected


async def test_retired_board_is_not_collected(session: Session) -> None:
    board = _board(session)
    board.status = "retired"
    session.flush()
    requests: list[str] = []
    gateway = _gateway(_answer(200, GREENHOUSE_BODY, requests))
    await collect_due(session, gateway, clock=lambda: NOW)
    await gateway.aclose()
    assert requests == []


async def test_one_worker_cycle_registers_then_collects(session: Session) -> None:
    _candidate(session)
    gateway = _gateway(_answer(200, GREENHOUSE_BODY))
    registration, collection = await run_cycle(session, gateway)
    await gateway.aclose()
    assert registration.registered == ["Acme"]
    assert collection.jobs == 2
