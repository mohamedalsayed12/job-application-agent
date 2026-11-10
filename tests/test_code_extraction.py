"""T10: board IDs only from code pattern matching on real URLs; Reed/Adzuna employers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction import (
    extract_and_persist_search_results,
    format_employer_snippet,
    parse_board_from_url,
)
from app.extraction.postings import extract_hit
from app.fetch.gateway import FetchGateway
from app.models import (
    Applicant,
    CandidateEvidence,
    CandidateState,
    ExtractedBy,
    SearchResult,
    SearchRun,
    SearchRunStatus,
    SearchTemplate,
)
from app.queue.names import clean_company_name
from app.search.reed import search_reed
from app.search.types import ProviderSuccess

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


def test_t10_for_query_ignored_on_non_embed_greenhouse_path() -> None:
    ref = parse_board_from_url(
        "https://boards.greenhouse.io/example/jobs/1?for=evil"
    )
    assert ref is not None
    assert ref.board_id == "example"


def test_clean_company_name_trailing_suffixes_only() -> None:
    assert clean_company_name("Example Corp Ltd.") == "example"
    assert clean_company_name("Democorp PLC") == "democorp"
    assert clean_company_name("The Co-operative Group") == "the co operative group"
    assert clean_company_name("Limited Edition Ltd") == "limited edition"


def test_em_dash_web_title_does_not_invent_employer() -> None:
    hit = extract_hit(
        url="https://boards.greenhouse.io/example/jobs/1",
        title="Senior Engineer — London",
        snippet="Hiring in London.",
    )
    assert hit is not None
    assert hit.board is not None
    assert hit.board.board_id == "example"
    assert hit.company_name is None


def test_employer_prefix_only_on_first_snippet_line() -> None:
    hit = extract_hit(
        url="https://www.reed.co.uk/jobs/1",
        title="Engineer",
        snippet="Nice role.\nemployer=ShouldNotWin",
    )
    assert hit is None


@pytest.mark.asyncio
async def test_reed_search_enriches_external_url_from_details() -> None:
    search_body = _load("reed_search_results.json")
    details = {
        "51234567": _load("reed_job_details_51234567.json"),
        "51234568": _load("reed_job_details_51234568.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(200, json=search_body)
        job_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=details[job_id])

    import ipaddress

    from app.core.config import Settings

    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"www.reed.co.uk"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),
    )
    outcome = await search_reed(
        gateway,
        keywords="engineer",
        location_name="London",
        settings=Settings(reed_api_key="test-reed-key"),  # type: ignore[arg-type]
    )
    await gateway.aclose()
    assert isinstance(outcome, ProviderSuccess)
    first, second = outcome.hits
    assert first.url.startswith("https://job-boards.greenhouse.io/")
    assert first.snippet.startswith("employer=Example Corp Ltd")
    hit = extract_hit(url=first.url, title=first.title, snippet=first.snippet)
    assert hit is not None
    assert hit.company_name == "Example Corp Ltd"
    assert hit.board is not None
    assert hit.board.board_id == "example"
    second_hit = extract_hit(url=second.url, title=second.title, snippet=second.snippet)
    assert second_hit is not None
    assert second_hit.company_name == "Democorp PLC"
    assert second_hit.board is None
    assert second.url.startswith("https://www.reed.co.uk/")


def test_adzuna_style_search_result_employer_and_lever_eu() -> None:
    payload = _load("adzuna_search_results.json")
    item = payload["results"][0]
    employer = item["company"]["display_name"]
    hit = extract_hit(
        url=item["redirect_url"],
        title=item["title"],
        snippet=format_employer_snippet(employer, item["description"]),
    )
    assert hit is not None
    assert hit.company_name == employer
    assert hit.board is not None
    assert hit.board.instance == "lever_eu"
    assert hit.board.board_id == "lever"


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

    board_ids = {(c.board_system, c.board_instance, c.board_id) for c in candidates}
    assert ("greenhouse", "greenhouse", "example") in board_ids
    assert ("lever", "lever_global", "leverdemo") in board_ids
    assert ("lever", "lever_eu", "lever") in board_ids
    assert all(c.extracted_by == ExtractedBy.code for c in candidates)
    assert all(c.state == CandidateState.queued for c in candidates)

    result_ids = {row.id for row in rows}
    for cand in candidates:
        if cand.board_id is None:
            continue
        ev = session.scalars(
            select(CandidateEvidence).where(CandidateEvidence.candidate_id == cand.id)
        ).all()
        assert ev
        assert any(e.search_result_id in result_ids for e in ev)


@pytest.mark.asyncio
async def test_t10_reed_and_adzuna_persist_via_search_results(session: Session) -> None:
    run = _seed_run(session)
    search_body = _load("reed_search_results.json")
    details = {
        "51234567": _load("reed_job_details_51234567.json"),
        "51234568": _load("reed_job_details_51234568.json"),
    }
    adzuna = _load("adzuna_search_results.json")

    import ipaddress

    from app.core.config import Settings

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(200, json=search_body)
        job_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=details[job_id])

    gateway = FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"www.reed.co.uk"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),
    )
    outcome = await search_reed(
        gateway,
        keywords="engineer",
        settings=Settings(reed_api_key="test-reed-key"),  # type: ignore[arg-type]
    )
    await gateway.aclose()
    assert isinstance(outcome, ProviderSuccess)

    rows: list[SearchResult] = []
    for rank, hit in enumerate(outcome.hits, start=1):
        row = SearchResult(
            search_run_id=run.id,
            rank=rank,
            url=hit.url,
            title=hit.title,
            snippet=hit.snippet,
        )
        session.add(row)
        rows.append(row)

    for rank, item in enumerate(adzuna["results"], start=100):
        employer = item["company"]["display_name"]
        row = SearchResult(
            search_run_id=run.id,
            rank=rank,
            url=item["redirect_url"],
            title=item["title"],
            snippet=format_employer_snippet(employer, item["description"]),
        )
        session.add(row)
        rows.append(row)
    session.flush()

    candidates = extract_and_persist_search_results(session, rows)
    session.flush()

    example = next(c for c in candidates if c.company_name == "Example Corp Ltd")
    assert example.board_system == "greenhouse"
    assert example.board_id == "example"
    eu = next(c for c in candidates if c.company_name == "Lever Demo EU Limited")
    assert eu.board_instance == "lever_eu"

    for cand in candidates:
        ev = session.scalars(
            select(CandidateEvidence).where(CandidateEvidence.candidate_id == cand.id)
        ).all()
        assert ev
        assert any(e.search_result_id is not None for e in ev)
