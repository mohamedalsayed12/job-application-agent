"""Agencies are recognised from what their homepage says about them."""

from __future__ import annotations

import ipaddress

import httpx
import pytest
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway
from app.models import Candidate, CandidateState, ExtractedBy
from app.queue import claim_next_candidate, ingest_candidate
from app.queue.agencies import homepage_says_agency, is_recruitment_agency
from app.verify import verify_candidate


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("TurnBlock - Emerging Tech Recruitment", ""),
        ("Miller Maxwell FinTech Recruiters", ""),
        ("UK's #1 Specialist Recruitment Company | Reed", ""),
        ("Recruitment Solutions for All Industries | Oscar's AI Job Match", "job matching platform"),
        ("Maxwell Bond | Empowering Careers", "Discover the leading recruitment agency for technology"),
        ("Acme", "We are an executive search firm for technology leaders"),
    ],
)
def test_agency_wording_is_recognised(title: str, description: str) -> None:
    assert homepage_says_agency(title, description)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Greenhouse | Orchestrated Hiring System", "Hiring software for growing teams"),
        ("AI-Powered Workforce Transformation Platform | Beamery", ""),
        ("Recruitment software for modern teams", ""),
        ("Software for recruiters | Acme", ""),
        ("The best recruitment platform | Acme", ""),
        ("Monzo", "We are recruiting engineers. Banking made easy."),
        ("Acme", "Our recruitment process takes two weeks."),
        ("Recruit Holdings", ""),
        ("Graduate Recruitment | Acme Bank", ""),
        ("Early Careers Recruitment | Acme", ""),
        ("Recruitment Marketing Platform | Acme", ""),
        ("AI Recruitment Automation | Acme", ""),
        ("", ""),
    ],
)
def test_other_companies_are_not_called_agencies(title: str, description: str) -> None:
    assert not homepage_says_agency(title, description)


@pytest.mark.parametrize("name", ["Turnblock.io", "Miller Maxwell", "Maxwell Bond", "Oscar Technology"])
def test_agencies_whose_sites_cannot_be_relied_on_are_named(name: str) -> None:
    assert is_recruitment_agency(name)


def _gateway(title: str, description: str = "") -> FetchGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            f'<html><head><title>{title}</title><meta name="description" content="{description}"></head>'
            "<body>Welcome</body></html>"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return FetchGateway(
        allowlist=frozenset(),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(handler),
    )


async def _verify(session: Session, name: str, gateway: FetchGateway) -> Candidate:
    result = ingest_candidate(
        session,
        company_name=name,
        extracted_by=ExtractedBy.code,
        domain_candidates=["acme-staff.example"],
        evidence=[(None, "quote", "https://example.test/1")],
    )
    session.flush()
    claimed = claim_next_candidate(session)
    assert claimed is not None and claimed.id == result.survivor.id
    await verify_candidate(session, claimed, gateway)
    await gateway.aclose()
    session.flush()
    return claimed


async def test_agency_homepage_ends_as_unsupported(session: Session) -> None:
    gateway = _gateway("Acme Staff | Specialist Recruitment Company")
    candidate = await _verify(session, "Acme Staff", gateway)
    assert candidate.state == CandidateState.unsupported
    assert candidate.last_error == "recruitment_agency"


async def test_agency_is_caught_even_when_the_title_does_not_match_the_name(session: Session) -> None:
    gateway = _gateway("Recruitment Solutions for All Industries")
    candidate = await _verify(session, "Acme Staff", gateway)
    assert candidate.state == CandidateState.unsupported
    assert candidate.last_error == "recruitment_agency"


async def test_employer_carrying_an_agency_domain_is_not_called_an_agency(session: Session) -> None:
    gateway = _gateway("Specialist Recruitment Company")
    candidate = await _verify(session, "Zeta Payments", gateway)
    assert candidate.last_error != "recruitment_agency"
    assert candidate.state == CandidateState.needs_review


async def test_agency_does_not_clash_with_the_company_holding_its_domain(session: Session) -> None:
    holder = ingest_candidate(
        session,
        company_name="Other Co",
        extracted_by=ExtractedBy.code,
        evidence=[(None, "quote", "https://example.test/other")],
    ).survivor
    session.flush()
    candidate = ingest_candidate(
        session,
        company_name="Acme Staff",
        extracted_by=ExtractedBy.code,
        domain_candidates=["acme-staff.example"],
        evidence=[(None, "quote", "https://example.test/acme")],
    ).survivor
    session.flush()
    candidate.primary_domain = None
    session.flush()
    holder.state = CandidateState.needs_review
    holder.primary_domain = "acme-staff.example"
    session.flush()
    claimed = claim_next_candidate(session)
    assert claimed is not None and claimed.id == candidate.id
    gateway = _gateway("Acme Staff | Specialist Recruitment Company")
    await verify_candidate(session, claimed, gateway)
    await gateway.aclose()
    session.flush()
    assert claimed.state == CandidateState.unsupported
    assert claimed.last_error == "recruitment_agency"
    assert claimed.primary_domain is None


async def test_company_with_a_board_goes_to_review_instead_of_being_removed(session: Session) -> None:
    candidate = ingest_candidate(
        session,
        company_name="Acme Staff",
        extracted_by=ExtractedBy.code,
        domain_candidates=["acme-staff.example"],
        board_system="greenhouse",
        board_instance="greenhouse",
        board_id="acmestaff",
        evidence=[(None, "quote", "https://example.test/acme")],
    ).survivor
    session.flush()
    claimed = claim_next_candidate(session)
    assert claimed is not None and claimed.id == candidate.id
    gateway = _gateway("Acme Staff | Specialist Recruitment Company")
    await verify_candidate(session, claimed, gateway)
    await gateway.aclose()
    session.flush()
    assert claimed.state == CandidateState.needs_review
    assert claimed.last_error == "possible_recruitment_agency"


async def test_named_agency_is_closed_without_fetching_its_site(session: Session) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"fetched {request.url}")

    candidate = ingest_candidate(
        session,
        company_name="Acme Staff",
        extracted_by=ExtractedBy.code,
        domain_candidates=["acme-staff.example"],
        evidence=[(None, "quote", "https://example.test/acme")],
    ).survivor
    session.flush()
    candidate.company_name = "Turnblock.io"
    session.flush()
    claimed = claim_next_candidate(session)
    assert claimed is not None and claimed.id == candidate.id
    gateway = FetchGateway(
        allowlist=frozenset(),
        resolve=lambda host: [ipaddress.IPv4Address("93.184.216.34")],
        transport=httpx.MockTransport(refuse),
    )
    await verify_candidate(session, claimed, gateway)
    await gateway.aclose()
    session.flush()
    assert claimed.state == CandidateState.unsupported
    assert claimed.last_error == "recruitment_agency"


async def test_ordinary_homepage_is_not_affected(session: Session) -> None:
    gateway = _gateway("Acme Staff | Payments")
    candidate = await _verify(session, "Acme Staff", gateway)
    assert candidate.last_error != "recruitment_agency"
