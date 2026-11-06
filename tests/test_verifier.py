"""T14–T18: Stage 5 verifier + admin review decisions."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from collections.abc import AsyncIterator, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.fetch.gateway import FetchGateway
from app.main import app
from app.models import (
    Candidate,
    CandidateState,
    EvidenceLevel,
    ExtractedBy,
    ReviewDecision,
    ReviewItem,
    VerificationCheck,
    VerificationOutcome,
)
from app.queue import (
    VERIFICATION_RETRY_DELAYS,
    claim_next_candidate,
    ingest_candidate,
)
from app.verify import apply_review_decision, verify_candidate
from app.verify.review import ReviewConflictError
from app.verify.types import UNSUPPORTED_REVISIT

ADMIN = "test-admin-key-step9"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "boards"

ALLOWLIST = frozenset(
    {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.eu.lever.co",
    }
)


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _html(title: str, body: str = "") -> bytes:
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head>"
        f"<body>{body}</body></html>"
    ).encode()


def _evidence(n: int = 1) -> list[tuple[None, str, str]]:
    return [(None, f"evidence quote {n}"[:200], f"https://example.test/{n}")]


def _gateway(handler) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=ALLOWLIST,
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),
    )


def _queue_board(
    session: Session,
    *,
    company: str,
    domain: str,
    system: str,
    instance: str,
    board_id: str,
    now: datetime,
) -> Candidate:
    result = ingest_candidate(
        session,
        company_name=company,
        extracted_by=ExtractedBy.code,
        primary_domain=domain,
        board_system=system,
        board_instance=instance,
        board_id=board_id,
        evidence=_evidence(),
        now=now,
    )
    session.flush()
    claimed = claim_next_candidate(session, now=now)
    assert claimed is not None
    assert claimed.id == result.survivor.id
    return claimed


@pytest.mark.asyncio
async def test_t14_official_link_and_empty_api_verifies(session: Session) -> None:
    """Board linked from official domain + working API (zero jobs) → verified."""
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    candidate = _queue_board(
        session,
        company="Example Corp",
        domain="examplecorp.test",
        system="greenhouse",
        instance="greenhouse",
        board_id="empty-board",
        now=now,
    )

    empty = _fixture("greenhouse_empty.json")

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "examplecorp.test" and path in {"/", ""}:
            return httpx.Response(
                200,
                content=_html(
                    "Example Corp",
                    '<a href="https://boards.greenhouse.io/empty-board">Jobs</a>',
                ),
                headers={"content-type": "text/html"},
            )
        if host == "examplecorp.test":
            return httpx.Response(404, headers={"content-type": "text/html"})
        if host == "boards-api.greenhouse.io":
            assert path == "/v1/boards/empty-board/jobs"
            return httpx.Response(
                200,
                content=empty,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    gateway = _gateway(handler)
    decision = await verify_candidate(session, candidate, gateway, now=now)
    await gateway.aclose()
    session.flush()

    assert decision.state == "verified"
    assert decision.evidence_level == EvidenceLevel.strong
    assert decision.last_job_count == 0
    assert candidate.state == CandidateState.verified
    assert candidate.primary_domain == "examplecorp.test"

    checks = session.scalars(
        select(VerificationCheck).where(
            VerificationCheck.candidate_id == candidate.id
        )
    ).all()
    assert {c.check_type for c in checks} >= {
        "official_domain",
        "ownership",
        "api_access",
    }
    assert all(
        c.outcome == VerificationOutcome.pass_
        for c in checks
        if c.check_type in {"official_domain", "ownership", "api_access"}
    )


@pytest.mark.asyncio
async def test_t15_greenhouse_name_match_without_link_needs_review(
    session: Session,
) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    candidate = _queue_board(
        session,
        company="Democorp",
        domain="democorp.test",
        system="greenhouse",
        instance="greenhouse",
        board_id="example",
        now=now,
    )
    body = _fixture("greenhouse_complete_example.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "democorp.test" and request.url.path in {"/", ""}:
            return httpx.Response(
                200,
                content=_html("Democorp Careers"),
                headers={"content-type": "text/html"},
            )
        if request.url.host == "democorp.test":
            return httpx.Response(404, headers={"content-type": "text/html"})
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    gateway = _gateway(handler)
    decision = await verify_candidate(session, candidate, gateway, now=now)
    await gateway.aclose()
    session.flush()

    assert decision.state == "needs_review"
    assert decision.evidence_level == EvidenceLevel.medium
    assert candidate.state == CandidateState.needs_review
    open_review = session.scalar(
        select(ReviewItem).where(
            ReviewItem.candidate_id == candidate.id,
            ReviewItem.decided_at.is_(None),
        )
    )
    assert open_review is not None
    assert "ownership_medium" in open_review.reason


@pytest.mark.asyncio
async def test_t16_timeout_retries_then_needs_review_never_unsupported(
    session: Session,
) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    candidate = _queue_board(
        session,
        company="Flaky Corp",
        domain="flakycorp.test",
        system="lever",
        instance="lever_global",
        board_id="flaky",
        now=now,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "flakycorp.test" and request.url.path in {"/", ""}:
            return httpx.Response(
                200,
                content=_html(
                    "Flaky Corp",
                    '<a href="https://jobs.lever.co/flaky">Careers</a>',
                ),
                headers={"content-type": "text/html"},
            )
        if request.url.host in {"api.lever.co", "api.eu.lever.co"}:
            raise httpx.ConnectTimeout("simulated", request=request)
        return httpx.Response(404, headers={"content-type": "text/html"})

    gateway = _gateway(handler)

    for step, delay in enumerate(VERIFICATION_RETRY_DELAYS):
        if candidate.state != CandidateState.verifying:
            claimed = claim_next_candidate(session, now=now)
            assert claimed is not None
            candidate = claimed
        decision = await verify_candidate(session, candidate, gateway, now=now)
        session.flush()
        assert decision.state == "retry_wait"
        assert candidate.state == CandidateState.retry_wait
        assert candidate.state not in (
            CandidateState.unsupported,
            CandidateState.rejected,
        )
        assert candidate.next_attempt_at == now + delay
        now = candidate.next_attempt_at

    claimed = claim_next_candidate(session, now=now)
    assert claimed is not None
    decision = await verify_candidate(session, claimed, gateway, now=now)
    await gateway.aclose()
    session.flush()
    assert decision.state == "needs_review"
    assert claimed.state == CandidateState.needs_review
    assert claimed.state not in (
        CandidateState.unsupported,
        CandidateState.rejected,
    )


@pytest.mark.asyncio
async def test_t17_workday_careers_unsupported_and_cached(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    result = ingest_candidate(
        session,
        company_name="Workday Co",
        extracted_by=ExtractedBy.code,
        primary_domain="workdayco.test",
        evidence=_evidence(),
        now=now,
    )
    session.flush()
    candidate = claim_next_candidate(session, now=now)
    assert candidate is not None
    assert candidate.id == result.survivor.id

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        host = request.url.host
        path = request.url.path
        if host == "workdayco.test" and path in {"/", ""}:
            return httpx.Response(
                200,
                content=_html(
                    "Workday Co",
                    '<a href="/careers">Careers</a>',
                ),
                headers={"content-type": "text/html"},
            )
        if host == "workdayco.test" and path.rstrip("/") == "/careers":
            return httpx.Response(
                200,
                content=_html(
                    "Workday Co Careers",
                    '<a href="https://workdayco.myworkdayjobs.com/en-US/Careers">'
                    "Open roles</a>",
                ),
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404, headers={"content-type": "text/html"})

    gateway = _gateway(handler)
    decision = await verify_candidate(session, candidate, gateway, now=now)
    await gateway.aclose()
    session.flush()

    assert decision.state == "unsupported"
    assert decision.careers_system == "workday"
    assert decision.revisit_at == now + UNSUPPORTED_REVISIT
    assert candidate.state == CandidateState.unsupported
    assert candidate.revisit_at == now + UNSUPPORTED_REVISIT
    checks = session.scalars(
        select(VerificationCheck).where(
            VerificationCheck.candidate_id == candidate.id,
            VerificationCheck.check_type == "unsupported_careers",
        )
    ).all()
    assert len(checks) == 1
    assert "myworkdayjobs.com" in (checks[0].evidence_snippet or "")

    # Discovery cache: not re-checked before revisit (dedupe step 4).
    again = ingest_candidate(
        session,
        company_name="Workday Co",
        extracted_by=ExtractedBy.code,
        primary_domain="workdayco.test",
        evidence=_evidence(2),
        now=now + timedelta(days=1),
    )
    session.flush()
    assert again.survivor.id == candidate.id
    assert again.survivor.state == CandidateState.unsupported
    assert (
        session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.primary_domain == "workdayco.test")
        )
        == 1
    )
    assert claim_next_candidate(session, now=now + timedelta(days=1)) is None


@pytest.mark.asyncio
async def test_t18_lever_global_not_found_tries_eu_then_verifies(
    session: Session,
) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    candidate = _queue_board(
        session,
        company="Lever Demo",
        domain="leverdemo.test",
        system="lever",
        instance="lever_global",
        board_id="lever",
        now=now,
    )
    not_found = _fixture("lever_not_found.json")
    eu_body = _fixture("lever_eu_complete_lever.json")
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if host == "leverdemo.test" and request.url.path in {"/", ""}:
            return httpx.Response(
                200,
                content=_html(
                    "Lever Demo",
                    '<a href="https://jobs.eu.lever.co/lever">Jobs</a>',
                ),
                headers={"content-type": "text/html"},
            )
        if host == "leverdemo.test":
            return httpx.Response(404, headers={"content-type": "text/html"})
        if host in {"api.lever.co", "api.eu.lever.co"}:
            seen_hosts.append(host)
            if host == "api.lever.co":
                return httpx.Response(
                    404,
                    content=not_found,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                content=eu_body,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    gateway = _gateway(handler)
    decision = await verify_candidate(session, candidate, gateway, now=now)
    await gateway.aclose()
    session.flush()

    assert seen_hosts == ["api.lever.co", "api.eu.lever.co"]
    assert decision.state == "verified"
    assert candidate.state == CandidateState.verified
    assert candidate.board_instance == "lever_eu"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(session: Session, admin_env: None) -> AsyncIterator[httpx.AsyncClient]:
    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _open_review_item(session: Session, company: str) -> tuple[Candidate, ReviewItem]:
    row = Candidate(
        company_name=company,
        cleaned_name=company.lower(),
        primary_domain=f"{company.lower().replace(' ', '')}.test",
        state=CandidateState.needs_review,
        extracted_by=ExtractedBy.code,
    )
    session.add(row)
    session.flush()
    item = ReviewItem(
        candidate_id=row.id,
        reason="ownership_medium",
        priority=30,
    )
    session.add(item)
    session.flush()
    return row, item


def test_review_decisions_approve_reject_unsupported(session: Session) -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    approved, approve_item = _open_review_item(session, "Approve Me")
    rejected, reject_item = _open_review_item(session, "Reject Me")
    unsupported, un_item = _open_review_item(session, "Unsupported Me")

    apply_review_decision(
        session,
        approve_item.id,
        decision=ReviewDecision.verified,
        reason="looks like their board",
        reviewer="mohammed",
        now=now,
    )
    apply_review_decision(
        session,
        reject_item.id,
        decision=ReviewDecision.rejected,
        reason="wrong company",
        reviewer="mohammed",
        now=now,
    )
    apply_review_decision(
        session,
        un_item.id,
        decision=ReviewDecision.unsupported,
        reason="custom ATS only",
        reviewer="mohammed",
        now=now,
    )
    session.flush()

    assert approved.state == CandidateState.verified
    assert approve_item.decision == ReviewDecision.verified
    assert approve_item.notes == "looks like their board"
    assert approve_item.decided_at == now

    assert rejected.state == CandidateState.rejected
    assert reject_item.decision == ReviewDecision.rejected

    assert unsupported.state == CandidateState.unsupported
    assert unsupported.revisit_at == now + UNSUPPORTED_REVISIT
    assert un_item.decision == ReviewDecision.unsupported

    with pytest.raises(ReviewConflictError):
        apply_review_decision(
            session,
            approve_item.id,
            decision=ReviewDecision.rejected,
            reason="already done",
            now=now,
        )

    _, empty_item = _open_review_item(session, "Need Reason")
    with pytest.raises(ReviewConflictError):
        apply_review_decision(
            session,
            empty_item.id,
            decision=ReviewDecision.rejected,
            reason="   ",
            now=now,
        )


@pytest.mark.asyncio
async def test_admin_review_list_and_decide(
    client: httpx.AsyncClient, session: Session
) -> None:
    _open_review_item(session, "Listed Co")
    session.flush()

    listed = await client.get(f"/admin/review?admin_key={ADMIN}")
    assert listed.status_code == 200
    assert "Listed Co" in listed.text
    assert "ownership_medium" in listed.text

    _, item = _open_review_item(session, "Html Decide")
    session.flush()
    decide = await client.post(
        f"/admin/review/{item.id}/decide?admin_key={ADMIN}",
        data={"decision": "verified", "reason": "confirmed manually"},
    )
    assert decide.status_code == 303
    session.refresh(item)
    assert item.decision == ReviewDecision.verified
    assert item.notes == "confirmed manually"
    assert item.decided_at is not None
