"""T10: board IDs only from code pattern matching on real URLs; Reed/Adzuna employers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction import (
    clean_company_name,
    extract_and_persist_adzuna_payload,
    extract_and_persist_reed_payload,
    extract_and_persist_search_results,
    parse_board_from_url,
)
from app.extraction.postings import extract_from_adzuna_payload, extract_from_reed_payload
from app.models import (
    Applicant,
    Candidate,
    CandidateEvidence,
    CandidateState,
    ExtractedBy,
    SearchResult,
    SearchRun,
    SearchRunStatus,
    SearchTemplate,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extraction"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("url", "system", "instance", "board_id"),
    [
        (
            "https://boards.greenhouse.io/example/jobs/44735",
            "greenhouse",
            "greenhouse",
            "example",
        ),
        (
            "https://job-boards.greenhouse.io/example/jobs/124185",
            "greenhouse",
            "greenhouse",
            "example",
        ),
        (
            "https://boards.greenhouse.io/embed/job_app?for=example&token=44735",
            "greenhouse",
            "greenhouse",
            "example",
        ),
        (
            "https://boards.greenhouse.io/example",
            "greenhouse",
            "greenhouse",
            "example",
        ),
        (
            "https://jobs.lever.co/leverdemo/ad208490-4052-4f91-a57e-433f2d1e484b",
            "lever",
            "lever_global",
            "leverdemo",
        ),
        (
            "https://jobs.eu.lever.co/lever/065f4538-7347-4207-909f-4ea68f63b4af",
            "lever",
            "lever_eu",
            "lever",
        ),
    ],
)
def test_t10_board_ids_from_real_fixture_urls(
    url: str, system: str, instance: str, board_id: str
) -> None:
    ref = parse_board_from_url(url)
    assert ref is not None
    assert ref.system == system
    assert ref.instance == instance
    assert ref.board_id == board_id


def test_t10_gh_jid_without_board_token_does_not_invent_board() -> None:
    assert parse_board_from_url("https://www.example.com/careers?gh_jid=44735") is None


def test_clean_company_name_strips_legal_suffixes() -> None:
    assert clean_company_name("Example Corp Ltd.") == "example"
    assert clean_company_name("Democorp PLC") == "democorp"


def test_reed_fixture_employer_and_apply_board() -> None:
    payload = _load("reed_search_results.json")
    hits = extract_from_reed_payload(payload)
    by_company = {h.company_name: h for h in hits}
    assert "Example Corp Ltd" in by_company
    assert by_company["Example Corp Ltd"].board is not None
    assert by_company["Example Corp Ltd"].board.board_id == "example"
    assert by_company["Example Corp Ltd"].apply_url.startswith(
        "https://job-boards.greenhouse.io/"
    ) or by_company["Example Corp Ltd"].apply_url.startswith(
        "https://boards.greenhouse.io/"
    )
    assert by_company["Democorp PLC"].board is None
    assert by_company["Democorp PLC"].apply_url.startswith("https://www.reed.co.uk/")


def test_adzuna_fixture_employer_and_lever_eu_apply() -> None:
    payload = _load("adzuna_search_results.json")
    hits = extract_from_adzuna_payload(payload)
    by_company = {h.company_name: h for h in hits}
    eu = by_company["Lever Demo EU Limited"]
    assert eu.board is not None
    assert eu.board.system == "lever"
    assert eu.board.instance == "lever_eu"
    assert eu.board.board_id == "lever"
    assert by_company["Acme Agency"].board is None


def _seed_run(session: Session) -> SearchRun:
    session.add(
        SearchTemplate(
            id="BD1",
            search_type="board_direct",
            provider="serpapi",
            pattern="fixture",
            status="unproven",
        )
    )
    applicant = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(applicant)
    session.flush()
    run = SearchRun(
        template_id="BD1",
        applicant_id=applicant.id,
        query="software engineer London site:boards.greenhouse.io",
        provider="serpapi",
        status=SearchRunStatus.succeeded,
        result_count=0,
    )
    session.add(run)
    session.flush()
    return run


def test_t10_persist_search_results_with_evidence(session: Session) -> None:
    run = _seed_run(session)
    serp = _load("serpapi_board_results.json")
    rows: list[SearchResult] = []
    for item in serp["organic_results"]:
        row = SearchResult(
            search_run_id=run.id,
            rank=int(item["position"]),
            url=item["link"],
            title=item["title"],
            snippet=item["snippet"],
        )
        session.add(row)
        rows.append(row)
    session.flush()

    candidates = extract_and_persist_search_results(session, rows)
    session.flush()

    # Careers page with only gh_jid must not create a board candidate.
    board_ids = {(c.board_system, c.board_instance, c.board_id) for c in candidates}
    assert ("greenhouse", "greenhouse", "example") in board_ids
    assert ("lever", "lever_global", "leverdemo") in board_ids
    assert ("lever", "lever_eu", "lever") in board_ids
    assert all(c.extracted_by == ExtractedBy.code for c in candidates)
    assert all(c.state == CandidateState.new for c in candidates)

    evidence = session.scalars(select(CandidateEvidence)).all()
    assert evidence
    result_ids = {row.id for row in rows}
    linked = [e for e in evidence if e.search_result_id in result_ids]
    assert linked
    # Every board candidate evidence points at the exact search_result URL it came from.
    for cand in candidates:
        if cand.board_id is None:
            continue
        ev = session.scalars(
            select(CandidateEvidence).where(CandidateEvidence.candidate_id == cand.id)
        ).all()
        assert ev
        assert any(e.search_result_id in result_ids for e in ev)


def test_t10_reed_and_adzuna_persist_evidence_links(session: Session) -> None:
    run = _seed_run(session)
    reed = _load("reed_search_results.json")
    adzuna = _load("adzuna_search_results.json")

    reed_ids: dict[str, uuid.UUID] = {}
    for rank, item in enumerate(reed["results"], start=1):
        apply_url = item.get("externalUrl") or item["jobUrl"]
        row = SearchResult(
            search_run_id=run.id,
            rank=rank,
            url=apply_url,
            title=f"{item['jobTitle']} — {item['employerName']}",
            snippet=item["jobDescription"],
        )
        session.add(row)
        session.flush()
        reed_ids[apply_url] = row.id

    adzuna_ids: dict[str, uuid.UUID] = {}
    for rank, item in enumerate(adzuna["results"], start=100):
        apply_url = item["redirect_url"]
        row = SearchResult(
            search_run_id=run.id,
            rank=rank,
            url=apply_url,
            title=item["title"],
            snippet=item["description"],
        )
        session.add(row)
        session.flush()
        adzuna_ids[apply_url] = row.id

    reed_cands = extract_and_persist_reed_payload(
        session, reed, search_result_ids=reed_ids
    )
    adzuna_cands = extract_and_persist_adzuna_payload(
        session, adzuna, search_result_ids=adzuna_ids
    )
    session.flush()

    assert any(c.company_name == "Example Corp Ltd" for c in reed_cands)
    example = next(c for c in reed_cands if c.company_name == "Example Corp Ltd")
    assert example.board_system == "greenhouse"
    assert example.board_id == "example"

    eu = next(c for c in adzuna_cands if "Lever Demo" in c.company_name)
    assert eu.board_instance == "lever_eu"

    for cand in (*reed_cands, *adzuna_cands):
        ev = session.scalars(
            select(CandidateEvidence).where(CandidateEvidence.candidate_id == cand.id)
        ).all()
        assert ev
        assert any(e.search_result_id is not None for e in ev)
