"""Step 10 part 2 rule changes (docs/step10-audit-findings.md)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.applicants.types import DiscoveryPreference
from app.core.config import Settings
from app.extraction.persist import persist_code_hits
from app.extraction.types import BoardRef, CodeHit
from app.models import Candidate
from app.queue.agencies import is_recruitment_agency
from app.search.queries import build_query
from app.verify.blocklist import is_aggregator_domain

_PREF = DiscoveryPreference(target_roles=("Graduate Software Engineer",), locations=("Manchester",), sector="tech")


def test_r6_defaults_leave_room_for_claude_output() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.ai_extraction_max_output_tokens == 1500
    assert cfg.ai_extraction_per_item_token_cap == 4000


def test_r4_cf3_query_asks_for_official_website_only() -> None:
    query = build_query("CF3", _PREF, employer_name="Octopus Energy")
    assert query.text == '"Octopus Energy" official website'
    assert "Manchester" not in query.text
    assert " OR " not in query.text


@pytest.mark.parametrize(
    "host",
    ["uk.jooble.org", "bebee.com", "torre.ai", "www.glassdoor.co.uk", "en.wikipedia.org", "www.facebook.com"],
)
def test_r4_cf3_noise_hosts_are_never_official(host: str) -> None:
    assert is_aggregator_domain(host)


def test_r4_real_company_domains_still_allowed() -> None:
    assert not is_aggregator_domain("octopus.energy")
    assert not is_aggregator_domain("matillion.com")


@pytest.mark.parametrize(
    ("name", "agency"),
    [
        ("Hays Specialist Recruitment Limited", True),
        ("Alex James Resourcing", True),
        ("ITOL Recruit", True),
        ("Robert Walters", True),
        ("Lorien", True),
        ("Picture More", True),
        ("Matillion", False),
        ("Octopus Energy", False),
        ("TalentLMS", False),
        ("Personnel Checks Ltd", False),
        ("Lorien Assisted Living", False),
        ("", False),
    ],
)
def test_agency_rule(name: str, agency: bool) -> None:
    assert is_recruitment_agency(name) is agency


def test_agencies_never_become_candidates(session: Session) -> None:
    hits = [
        CodeHit(company_name="VIQU IT Recruitment", board=None, quoted_text="VIQU IT Recruitment", source_url="https://www.reed.co.uk/jobs/1"),
        CodeHit(company_name="Hays", board=None, quoted_text="Hays", source_url="https://www.reed.co.uk/jobs/2"),
        CodeHit(company_name="Matillion", board=None, quoted_text="Matillion", source_url="https://www.reed.co.uk/jobs/3"),
        CodeHit(
            company_name=None,
            board=BoardRef(system="lever", instance="lever_global", board_id="octoenergy"),
            quoted_text="jobs.lever.co/octoenergy",
            source_url="https://jobs.lever.co/octoenergy",
        ),
    ]
    touched = persist_code_hits(session, hits)
    names = sorted(c.company_name for c in session.scalars(select(Candidate)))
    assert names == ["Matillion", "octoenergy"]
    assert len(touched) == 2


# --- PR-B: R1 / R2 / R4 filter / R5 -------------------------------------------------

import ipaddress  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from decimal import Decimal  # noqa: E402

import httpx  # noqa: E402

from app.discovery.cf3 import search_official_domain  # noqa: E402
from app.fetch.gateway import FetchGateway  # noqa: E402
from app.models import Applicant, BudgetPolicy, CandidateState, ExtractedBy  # noqa: E402
from app.queue import claim_next_candidate, ingest_candidate  # noqa: E402
from app.queue.names import same_company  # noqa: E402
from app.search import ensure_search_templates  # noqa: E402

_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _lever_board(session: Session, slug: str = "octoenergy") -> Candidate:
    return ingest_candidate(
        session,
        company_name=slug,
        extracted_by=ExtractedBy.code,
        board_system="lever",
        board_instance="lever_global",
        board_id=slug,
        now=_NOW,
    ).survivor


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Octopus Energy Group", "Octopus Energy", True),
        ("matillion", "Matillion", True),
        ("BAE Systems", "BAE Systems plc", True),
        ("Lorien", "Lorien Assisted Living", False),
        ("Arm", "Armstrong", False),
        ("Group", "Group", False),
    ],
)
def test_same_company(a: str, b: str, same: bool) -> None:
    assert same_company(a, b) is same


def test_r2_domainless_candidate_waits_for_cf3(session: Session) -> None:
    board = _lever_board(session)
    session.flush()
    assert claim_next_candidate(session, now=_NOW) is None

    board.domain_searched_at = _NOW
    session.flush()
    claimed = claim_next_candidate(session, now=_NOW)
    assert claimed is not None and claimed.id == board.id


def test_r2_candidate_with_domain_is_claimable_at_once(session: Session) -> None:
    row = ingest_candidate(
        session,
        company_name="Matillion",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["matillion.com"],
        now=_NOW,
    ).survivor
    session.flush()
    claimed = claim_next_candidate(session, now=_NOW)
    assert claimed is not None and claimed.id == row.id


def test_r1_cf3_hit_enriches_its_target(session: Session) -> None:
    board = _lever_board(session, "matillion")
    result = ingest_candidate(
        session,
        company_name="Matillion",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["matillion.com"],
        now=_NOW,
        cf3_target=board,
    )
    assert result.survivor.id == board.id
    assert result.reason == "cf3_target"
    assert board.domain_candidates == ["matillion.com"]
    assert board.board_id == "matillion"
    assert session.scalar(select(func.count()).select_from(Candidate)) == 1


def test_r1_different_company_does_not_enrich_target(session: Session) -> None:
    target = ingest_candidate(session, company_name="Lorien", extracted_by=ExtractedBy.code, now=_NOW).survivor
    other = ingest_candidate(
        session,
        company_name="Lorien Assisted Living",
        extracted_by=ExtractedBy.ai,
        domain_candidates=["lorienassistedliving.example"],
        now=_NOW,
        cf3_target=target,
    ).survivor
    assert other.id != target.id
    assert target.domain_candidates == []


def _cf3_gateway(seen: list[str]) -> FetchGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "jobs.lever.co":
            return httpx.Response(200, html="<html><title>Octopus Energy Group</title></html>")
        if request.url.host == "serpapi.com":
            assert request.url.params["q"] == '"Octopus Energy Group" official website'
            return httpx.Response(
                200,
                json={
                    "search_metadata": {"status": "Success"},
                    "organic_results": [
                        {"position": 1, "link": "https://uk.jooble.org/jobs-octopus", "title": "Octopus jobs", "snippet": "Jobs at Octopus Energy"},
                        {"position": 2, "link": "https://octopus.energy/", "title": "Octopus Energy", "snippet": "Octopus Energy: the UK's greenest supplier."},
                    ],
                },
            )
        if request.url.host == "api.anthropic.com":
            body = json.loads(request.content.decode("utf-8"))
            data = body["messages"][0]["content"]
            assert "jooble" not in data  # R4: noise hosts never reach Claude
            rows = json.loads(next(line for line in data.splitlines() if line.startswith("[")))
            rid = rows[0]["result_id"]
            employers = [{
                "company_name": "Octopus Energy",
                "domain_candidates": ["octopus.energy"],
                "evidence": [{"result_id": rid, "quote": "Octopus Energy"}],
            }]
            return httpx.Response(200, json={
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": json.dumps({"employers": employers})}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 40},
            })
        raise AssertionError(f"unexpected {request.url}")

    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"jobs.lever.co", "serpapi.com", "api.anthropic.com"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_r1_r4_r5_cf3_end_to_end(session: Session) -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        web_search_provider="serpapi",
        serpapi_api_key="test-serpapi",
        ai_provider="anthropic",
        anthropic_api_key="test-anthropic",
    )
    ensure_search_templates(session, cfg)
    session.add(BudgetPolicy(provider="*", period="month", money_cap=Decimal("2.00"), request_cap=None,
                             currency="GBP", effective_from=_NOW, effective_to=None))
    applicant = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(applicant)
    board = _lever_board(session)
    session.flush()

    seen: list[str] = []
    gateway = _cf3_gateway(seen)
    try:
        outcome = await search_official_domain(session, gateway, board, applicant_id=applicant.id, pref=_PREF, settings=cfg)
    finally:
        await gateway.aclose()

    assert outcome.status == "searched"
    assert board.company_name == "Octopus Energy Group"  # R5
    assert board.board_id == "octoenergy"
    assert outcome.domains == ("octopus.energy",)  # R1: same row, not a new one
    assert board.domain_searched_at is not None  # R2
    assert board.state == CandidateState.queued
    assert session.scalar(select(func.count()).select_from(Candidate)) == 1
    assert seen[0] == "jobs.lever.co/octoenergy"


# --- PR-C: R3 / R7 ------------------------------------------------------------------

from app.verify import verify_candidate  # noqa: E402
from app.verify.domain import names_match  # noqa: E402
from tests.test_verifier import _evidence, _gateway, _html  # noqa: E402

_PADDING = "<p>We are hiring across engineering, design and operations.</p>" * 40


@pytest.mark.parametrize(
    ("company", "title", "match"),
    [
        ("Octopus Energy Group", "Octopus Energy: The UK's greenest energy supplier", True),
        ("Octopus Energy Group", "Home | Octopus Energy", True),
        ("Matillion", "Matillion \u2013 Data Productivity Cloud", True),
        ("Lorien Assisted Living", "Lorien | IT recruitment", False),
        ("Arm", "Armstrong Ltd", False),
        ("Acme", "Welcome", False),
    ],
)
def test_r7_title_matches_brand_segment(company: str, title: str, match: bool) -> None:
    assert names_match(company, title) is match


async def _verify_site(session: Session, company: str, pages: dict[str, bytes]):
    row = ingest_candidate(
        session,
        company_name=company,
        extracted_by=ExtractedBy.code,
        primary_domain="site.test",
        evidence=_evidence(),
        now=_NOW,
    ).survivor
    session.flush()
    claimed = claim_next_candidate(session, now=_NOW)
    assert claimed is not None and claimed.id == row.id

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path.rstrip("/") or "/")
        if request.url.host != "site.test" or body is None:
            return httpx.Response(404, headers={"content-type": "text/html"})
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    gateway = _gateway(handler)
    try:
        return await verify_candidate(session, claimed, gateway, now=_NOW)
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_r3_careers_page_without_board_is_unsupported(session: Session) -> None:
    decision = await _verify_site(session, "Acme Widgets", {
        "/": _html("Acme Widgets | Home", '<a href="/careers">Careers</a>'),
        "/careers": _html("Careers at Acme Widgets", "<p>Email cv@acme.test to apply.</p>" + _PADDING),
    })
    assert decision.state == "unsupported"
    assert decision.reason == "unsupported_careers:no_public_board"


@pytest.mark.asyncio
async def test_r3_new_ats_marker_is_unsupported(session: Session) -> None:
    decision = await _verify_site(session, "Acme Widgets", {
        "/": _html("Acme Widgets", '<a href="https://acme.teamtailor.com/jobs">Jobs</a>'),
    })
    assert decision.state == "unsupported"
    assert decision.careers_system == "teamtailor"


@pytest.mark.asyncio
async def test_r3_board_link_stays_for_review(session: Session) -> None:
    decision = await _verify_site(session, "Acme Widgets", {
        "/": _html("Acme Widgets", '<a href="/careers">Careers</a>'),
        "/careers": _html("Careers", '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>' + _PADDING),
    })
    assert decision.state == "needs_review"
    assert decision.reason == "board_link_on_careers_page"


@pytest.mark.asyncio
async def test_r3_no_careers_page_stays_for_review(session: Session) -> None:
    decision = await _verify_site(session, "Acme Widgets", {"/": _html("Acme Widgets")})
    assert decision.state == "needs_review"
    assert decision.reason == "no_supported_public_board"


@pytest.mark.asyncio
async def test_r3_tiny_careers_stub_stays_for_review(session: Session) -> None:
    decision = await _verify_site(session, "Acme Widgets", {
        "/": _html("Acme Widgets"),
        "/careers": _html("Careers", '<div id="root"></div>'),
    })
    assert decision.state == "needs_review"


# --- PR-D: discovery pass -----------------------------------------------------------

from app.discovery.run import _waiting_for_cf3, run_discovery  # noqa: E402
from app.models import SearchRun, SearchRunStatus  # noqa: E402


def test_cf3_waits_only_for_unsearched_domainless_candidates(session: Session) -> None:
    waiting = ingest_candidate(session, company_name="Acme Widgets", extracted_by=ExtractedBy.code, now=_NOW).survivor
    searched = ingest_candidate(session, company_name="Searched Co", extracted_by=ExtractedBy.code, now=_NOW).survivor
    searched.domain_searched_at = _NOW
    ingest_candidate(session, company_name="Has Domain", extracted_by=ExtractedBy.ai,
                     domain_candidates=["hasdomain.test"], now=_NOW)
    session.flush()
    assert [c.id for c in _waiting_for_cf3(session)] == [waiting.id]


@pytest.mark.asyncio
async def test_discovery_pass_with_no_applicants_does_nothing(session: Session) -> None:
    gateway = _gateway(lambda request: httpx.Response(500))
    try:
        report = await run_discovery(session, gateway, settings=Settings(_env_file=None))  # type: ignore[call-arg]
    finally:
        await gateway.aclose()
    assert not report.searches and not report.cf3 and not report.verified


def test_rerun_resumes_planned_cold_start_runs(session: Session) -> None:
    from app.discovery.run import _planned_runs

    ensure_search_templates(session, Settings(_env_file=None))  # type: ignore[call-arg]
    applicant = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(applicant)
    session.flush()
    cold = SearchRun(template_id="CF2", applicant_id=applicant.id, query="q", provider="serpapi",
                     status=SearchRunStatus.planned, attempt_count=0)
    cf3 = SearchRun(template_id="CF3", applicant_id=applicant.id, query="q3", provider="serpapi",
                    status=SearchRunStatus.planned, attempt_count=0)
    session.add_all([cold, cf3])
    session.flush()
    assert [r.id for r in _planned_runs(session, applicant.id)] == [cold.id]


@pytest.mark.asyncio
async def test_cf3_reuses_an_earlier_search_for_the_same_query(session: Session) -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        web_search_provider="serpapi",
        serpapi_api_key="test-serpapi",
        ai_provider="anthropic",
        anthropic_api_key="test-anthropic",
    )
    ensure_search_templates(session, cfg)
    session.add(BudgetPolicy(provider="*", period="month", money_cap=Decimal("2.00"), request_cap=None,
                             currency="GBP", effective_from=_NOW, effective_to=None))
    applicant = Applicant(email=f"{uuid.uuid4()}@example.com")
    session.add(applicant)
    board = _lever_board(session)
    session.flush()

    seen: list[str] = []
    gateway = _cf3_gateway(seen)
    try:
        await search_official_domain(session, gateway, board, applicant_id=applicant.id, pref=_PREF, settings=cfg)
        board.domain_searched_at = None  # as if the AI step had waited
        board.domain_candidates = []
        await search_official_domain(session, gateway, board, applicant_id=applicant.id, pref=_PREF, settings=cfg)
    finally:
        await gateway.aclose()

    assert sum(1 for s in seen if s.startswith("serpapi.com")) == 1


@pytest.mark.asyncio
async def test_discovery_leaves_runs_alone_until_their_retry_time(session: Session) -> None:
    from datetime import timedelta

    from app.applicants.service import create_applicant, parse_preference_input

    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    ensure_search_templates(session, cfg)
    applicant = create_applicant(
        session, email=f"{uuid.uuid4()}@example.com", display_name="Due Test",
        preference=parse_preference_input(target_roles=["Software Engineer"], locations=["Leeds"]),
    )
    waiting = SearchRun(template_id="CF2", applicant_id=applicant.id, query="q", provider="serpapi",
                        status=SearchRunStatus.planned, attempt_count=0,
                        next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
    session.add(waiting)
    session.flush()
    requests: list[str] = []
    gateway = _gateway(lambda request: requests.append(str(request.url)) or httpx.Response(500))
    try:
        report = await run_discovery(session, gateway, settings=cfg)
    finally:
        await gateway.aclose()
    assert report.searches == {"not_due": 1}
    assert requests == []
    assert session.scalar(select(func.count()).select_from(SearchRun)) == 1


def test_bd1_keeps_new_greenhouse_board_hosts() -> None:
    from app.search.runner import _filter_hits
    from app.search.templates import GREENHOUSE_SITE
    from app.search.types import SearchHit

    urls = [
        "https://job-boards.greenhouse.io/databricks/jobs/1",
        "https://job-boards.eu.greenhouse.io/rightmovecareers/jobs/2",
        "https://boards.greenhouse.io/acme/jobs/3",
        "https://uk.indeed.com/jobs?q=greenhouse.io",
    ]
    hits = tuple(SearchHit(url=u, title="t", snippet="s") for u in urls)
    kept = [h.url for h in _filter_hits(hits, site=GREENHOUSE_SITE)]
    assert kept == urls[:3]


# --- Direct board lookup ------------------------------------------------------------

from app.discovery.boards import attach_public_board, board_slugs  # noqa: E402
from app.models import ReviewItem  # noqa: E402


def _board_api(boards, lever=None, site=None) -> FetchGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "boards-api.greenhouse.io":
            slug = path.split("/")[3]
            if slug not in boards:
                return httpx.Response(404, json={"status": 404})
            if path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Engineer", "company_name": boards[slug]}]})
            return httpx.Response(200, json={"name": boards[slug], "content": ""})
        if host == "api.lever.co" and path.split("/")[3] in (lever or {}):
            return httpx.Response(200, json=[])
        if host == "jobs.lever.co" and path.strip("/") in (lever or {}):
            return httpx.Response(200, content=_html(lever[path.strip("/")]), headers={"content-type": "text/html"})
        body = (site or {}).get(path.rstrip("/") or "/")
        if host == "acme.test" and body is not None:
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        return httpx.Response(404, headers={"content-type": "text/html"})

    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "jobs.lever.co", "jobs.eu.lever.co"}),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def _needs_review(session: Session, name: str = "Acme Widgets", domain: str = "acme.test") -> Candidate:
    row = ingest_candidate(session, company_name=name, extracted_by=ExtractedBy.code, primary_domain=domain,
                           evidence=_evidence(), now=_NOW).survivor
    row.state = CandidateState.needs_review
    row.next_attempt_at = None
    session.add(ReviewItem(candidate_id=row.id, reason="no_supported_public_board", priority=45))
    session.flush()
    return row


def _reviews(session: Session, row: Candidate) -> int:
    return session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.candidate_id == row.id)) or 0


def test_board_slugs_use_name_and_domain(session: Session) -> None:
    row = _needs_review(session, "Octopus Energy Group", "octopus.energy")
    assert board_slugs(row)[:2] == ["octopusenergy", "octopus-energy"]
    assert "octopus" in board_slugs(row)


@pytest.mark.asyncio
async def test_lookup_attaches_board_and_requeues(session: Session) -> None:
    row = _needs_review(session)
    gateway = _board_api({"acmewidgets": "Acme Widgets"})
    try:
        found = await attach_public_board(session, gateway, row)
    finally:
        await gateway.aclose()
    assert found is not None
    assert (row.board_system, row.board_instance, row.board_id) == ("greenhouse", "greenhouse", "acmewidgets")
    assert row.state == CandidateState.queued
    assert _reviews(session, row) == 0
    assert claim_next_candidate(session) is not None


@pytest.mark.asyncio
async def test_lookup_rejects_a_board_with_another_companys_name(session: Session) -> None:
    row = _needs_review(session, "Oscar Technology", "acme.test")
    gateway = _board_api({"acme": "Oscar Health", "oscartechnology": "Oscar Health"})
    try:
        found = await attach_public_board(session, gateway, row)
    finally:
        await gateway.aclose()
    assert found is None
    assert row.board_id is None
    assert row.state == CandidateState.needs_review
    assert _reviews(session, row) == 1


@pytest.mark.asyncio
async def test_lookup_reads_lever_name_from_board_page(session: Session) -> None:
    row = _needs_review(session)
    gateway = _board_api({}, lever={"acmewidgets": "Acme Widgets"})
    try:
        await attach_public_board(session, gateway, row)
    finally:
        await gateway.aclose()
    assert (row.board_system, row.board_instance) == ("lever", "lever_global")


@pytest.mark.asyncio
async def test_lookup_skips_a_board_another_candidate_holds(session: Session) -> None:
    holder = ingest_candidate(session, company_name="acmewidgets", extracted_by=ExtractedBy.code,
                              board_system="greenhouse", board_instance="greenhouse",
                              board_id="acmewidgets", now=_NOW).survivor
    row = _needs_review(session)
    gateway = _board_api({"acmewidgets": "Acme Widgets"})
    try:
        found = await attach_public_board(session, gateway, row)
    finally:
        await gateway.aclose()
    assert found is None
    assert row.board_id is None
    assert holder.board_id == "acmewidgets"


@pytest.mark.parametrize(
    ("homepage", "state"),
    [
        ('<a href="https://job-boards.greenhouse.io/acmewidgets">Jobs</a>', "verified"),
        ("<p>No link to any board here.</p>", "needs_review"),
    ],
)
@pytest.mark.asyncio
async def test_found_board_verifies_only_when_the_site_links_to_it(
    session: Session, homepage: str, state: str
) -> None:
    row = _needs_review(session)
    gateway = _board_api({"acmewidgets": "Acme Widgets"}, site={"/": _html("Acme Widgets", homepage)})
    try:
        await attach_public_board(session, gateway, row)
        session.flush()
        claimed = claim_next_candidate(session)
        assert claimed is not None
        assert claimed.id == row.id
        decision = await verify_candidate(session, claimed, gateway)
    finally:
        await gateway.aclose()
    assert decision.state == state
