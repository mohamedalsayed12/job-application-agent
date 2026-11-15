"""D55: fake-listing verification (Companies House, domain, careers page, scam text)."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.matching.match import showable
from app.models import JobVerification, ListingVerdict
from app.quality.checks import Confidence, VerificationSignal, check_real
from app.quality.report import check_all, store_results
from app.verify.domain_search import parse_ddg_result_urls
from app.verify.run import verify_jobs
from app.verify.scam_text import scam_reasons
from tests.jobs import _job, _li, _store
from tests.test_quality import ADVERT, _facts

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _public_resolve(host: str) -> list[ipaddress.IPv4Address]:
    return [ipaddress.IPv4Address("93.184.216.34")]


def _settings(**changes: object) -> Settings:
    base = Settings(
        companies_house_api_key="test-ch-key",
        api_allowlist="api.company-information.service.gov.uk",
        verify_per_cycle=50,
    )
    return base.model_copy(update=changes)


def _gateway(handler) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.company-information.service.gov.uk"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),
    )


def _ch_items(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"items": list(rows)})


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Get rich quick working from home.", ("promises quick wealth",)),
        ("Build APIs. No fees.", ()),
        ("Must be of legal age and know how to type. That's all — no experience needed.", ("vague requirements only",)),
    ],
)
def test_scam_text_extra_heuristics(text: str, expected: tuple[str, ...]) -> None:
    assert scam_reasons(text) == expected


def test_get_rich_lowers_real_confidence() -> None:
    confidence, reasons = check_real(_facts(description="Get rich quick with our scheme. " + ADVERT))
    assert confidence == Confidence.low
    assert any("wealth" in reason for reason in reasons)


def test_verification_fake_forces_low() -> None:
    signal = VerificationSignal(outcome="fake", listing_found=False, reasons=("company not trading",))
    confidence, reasons = check_real(_facts(), signal)
    assert confidence == Confidence.low
    assert "company not trading" in reasons


def test_verification_real_listing_raises_high() -> None:
    signal = VerificationSignal(outcome="real", listing_found=True, reasons=("confirmed on company site",))
    confidence, reasons = check_real(_facts(), signal)
    assert confidence == Confidence.high
    assert "confirmed on company site" in reasons


def test_empty_anchor_does_not_steal_apply_url() -> None:
    from app.verify.listing import _best_apply_url

    html = (
        '<a href="/careers/backend-developer">Backend Developer</a>'
        '<a href="https://evil.example/phish"></a>'
        '<a href="https://acme.example/x">a</a>'
    )
    assert _best_apply_url(html, "backend developer", "https://acme.example/careers") == (
        "https://acme.example/careers/backend-developer"
    )


def test_registrable_domain_rejects_public_suffixes() -> None:
    from app.verify.domain_search import registrable_domain

    assert registrable_domain("www.acme.com.au") == "acme.com.au"
    assert registrable_domain("com.au") is None
    assert registrable_domain("evil.com.au") == "evil.com.au"


def test_parse_ddg_uddg_links() -> None:
    html = (
        '<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2F">'
        "Acme</a>"
        '<a href="https://other.example/about">Other</a>'
    )
    assert parse_ddg_result_urls(html) == ["https://acme.example/", "https://other.example/about"]


@pytest.mark.asyncio
async def test_dissolved_company_is_fake_and_not_showable(session: Session) -> None:
    _store(session, [_li(1, company="Gone Ltd", content=ADVERT, title="Backend Developer")])
    job = _job(session, 1)
    assert job is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if "company-information.service.gov.uk" in request.url.host:
            return _ch_items(
                {"company_number": "12345678", "title": "GONE LTD", "company_status": "dissolved"}
            )
        return httpx.Response(404)

    gateway = _gateway(handler)
    report = await verify_jobs(
        session, gateway=gateway, settings=_settings(), persist=session.flush, now=NOW
    )
    await gateway.aclose()
    assert report.fake == 1
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    assert row is not None and row.outcome == ListingVerdict.fake
    store_results(session, now=NOW)
    assert job.id not in {facts.job_id for facts in showable(session, NOW)}


@pytest.mark.asyncio
async def test_listing_on_company_site_upgrades_apply_url(session: Session) -> None:
    _store(
        session,
        [
            _li(
                2,
                company="Acme Limited",
                content=ADVERT,
                title="Backend Developer",
                applyUrl="https://www.linkedin.com/jobs/view/2",
            )
        ],
    )
    job = _job(session, 2)
    assert job is not None
    linkedin_url = job.apply_url

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if "company-information.service.gov.uk" in host:
            return _ch_items(
                {"company_number": "00445790", "title": "ACME LIMITED", "company_status": "active"}
            )
        if "duckduckgo.com" in host:
            return _html(
                '<a class="result__a" '
                'href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2F">Acme</a>'
            )
        if host == "acme.example":
            if path in ("/", ""):
                return _html(
                    "<html><body>Welcome to Acme Limited. <a href='/careers'>Careers</a></body></html>"
                )
            if "career" in path or "job" in path:
                return _html(
                    "<html><body><a href='/careers/backend-developer'>Backend Developer</a>"
                    " Join Acme Limited.</body></html>"
                )
        return httpx.Response(404)

    gateway = _gateway(handler)
    report = await verify_jobs(
        session, gateway=gateway, settings=_settings(), persist=session.flush, now=NOW
    )
    await gateway.aclose()
    assert report.real == 1
    assert report.upgraded == 1
    session.refresh(job)
    assert job.apply_url.startswith("https://acme.example/")
    assert job.apply_url != linkedin_url
    assert (job.field_provenance.get("apply_url") or {}).get("source") == "verification"
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    assert row is not None and row.outcome == ListingVerdict.real and row.listing_found
    store_results(session, now=NOW)
    quality = next(q for facts, q in check_all(session, NOW) if facts.job_id == job.id)
    assert quality.confidence == Confidence.high


@pytest.mark.asyncio
async def test_active_company_without_listing_stays_medium(session: Session) -> None:
    _store(session, [_li(3, company="Acme Limited", content=ADVERT, title="Backend Developer")])
    job = _job(session, 3)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if "company-information.service.gov.uk" in host:
            return _ch_items(
                {"company_number": "00445790", "title": "ACME LIMITED", "company_status": "active"}
            )
        if "duckduckgo.com" in host:
            return _html(
                '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2F">Acme</a>'
            )
        if host == "acme.example":
            return _html("<html><body>Acme Limited home. No openings right now.</body></html>")
        return httpx.Response(404)

    gateway = _gateway(handler)
    report = await verify_jobs(
        session, gateway=gateway, settings=_settings(), persist=session.flush, now=NOW
    )
    await gateway.aclose()
    assert report.inconclusive == 1
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    assert row is not None
    assert row.outcome == ListingVerdict.inconclusive
    assert row.listing_found is False
    store_results(session, now=NOW)
    quality = next(q for facts, q in check_all(session, NOW) if facts.job_id == job.id)
    assert quality.confidence == Confidence.medium
    assert job.id in {facts.job_id for facts in showable(session, NOW)}


@pytest.mark.asyncio
async def test_missing_companies_house_key_does_not_crash(session: Session) -> None:
    _store(session, [_li(4, company="Acme Limited", content=ADVERT)])
    job = _job(session, 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if "duckduckgo.com" in request.url.host:
            return _html("<html></html>")
        if request.url.host == "acme.example":
            return _html("<html><body>Acme</body></html>")
        return httpx.Response(404)

    gateway = _gateway(handler)
    report = await verify_jobs(
        session,
        gateway=gateway,
        settings=_settings(companies_house_api_key=""),
        persist=session.flush,
        now=NOW,
    )
    await gateway.aclose()
    assert report.checked == 1
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    assert row is not None and row.outcome == ListingVerdict.inconclusive


@pytest.mark.asyncio
async def test_scam_text_marks_fake_without_network(session: Session) -> None:
    _store(session, [_li(5, company="Scam Co", content="Get rich quick! " + ADVERT)])
    job = _job(session, 5)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    gateway = _gateway(handler)
    report = await verify_jobs(
        session, gateway=gateway, settings=_settings(), persist=session.flush, now=NOW
    )
    await gateway.aclose()
    assert report.fake == 1
    assert seen == []
    row = session.scalar(select(JobVerification).where(JobVerification.job_id == job.id))
    assert row is not None and row.outcome == ListingVerdict.fake
