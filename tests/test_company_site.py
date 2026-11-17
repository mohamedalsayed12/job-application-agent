"""Keywords come from the employer's own site, and only after the homepage names them."""

import httpx
import pytest
from sqlalchemy.orm import Session

from app.fetch.gateway import FetchGateway
from app.tailor.company import NO_TITLE, employer_domain, load_site_terms, terms_from_text, title_names_company
from tests.jobs import _public_resolve
from tests.test_matching import SOON

_PAGE = (
    "<html><head><title>Acme</title></head><body>"
    + " ".join(["Payments platform reliability"] * 30)
    + "</body></html>"
).encode()


def test_a_title_must_contain_the_company_name_as_a_word() -> None:
    assert title_names_company("Acme Careers", "Acme")
    assert not title_names_company("Homepage Email Database", "Home Mail Data")


def test_a_job_board_link_is_not_the_employer_site() -> None:
    assert employer_domain("https://uk.linkedin.com/jobs/view/1", "", "https://uk.linkedin.com/jobs/1") is None
    assert employer_domain("https://uk.linkedin.com/jobs/1", "https://www.acme.example/about", "") == "acme.example"


def test_repeated_words_keep_their_first_casing() -> None:
    assert terms_from_text("Payments payments platform platform", company_name="Acme") == ["Payments", "platform"]


def _gateway(handler) -> FetchGateway:
    return FetchGateway(
        user_agent="JobApplicationAgent-Test/0.1",
        allowlist=frozenset({"api.anthropic.com"}),
        resolve=_public_resolve,
        transport=httpx.MockTransport(handler),
    )


async def test_a_site_whose_title_names_someone_else_is_ignored(session: Session) -> None:
    page = _PAGE.replace(b"<title>Acme</title>", b"<title>Other Holdings</title>")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=page)

    site = await load_site_terms(
        session,
        _gateway(handler),
        company_name="Acme",
        apply_url="https://acme.example/jobs/1",
        named_url="",
        posting="",
        now=SOON,
    )

    assert site.terms == []
    assert site.note == NO_TITLE


async def test_wording_is_cached_after_the_homepage_names_the_company(session: Session) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=_PAGE)

    gateway = _gateway(handler)
    first = await load_site_terms(
        session,
        gateway,
        company_name="Acme",
        apply_url="https://uk.linkedin.com/jobs/1",
        named_url="https://www.acme.example/",
        posting="",
        now=SOON,
    )
    fetched = [path for path in paths if path != "/robots.txt"]
    paths.clear()

    second = await load_site_terms(
        session,
        gateway,
        company_name="Acme",
        apply_url="https://uk.linkedin.com/jobs/1",
        named_url="https://www.acme.example/",
        posting="",
        now=SOON,
    )

    assert first.terms == ["Payments", "platform", "reliability"]
    assert second.terms == first.terms
    assert fetched
    assert paths == []


@pytest.mark.parametrize("apply_url", ["https://uk.linkedin.com/jobs/1", ""])
async def test_no_employer_site_is_said_out_loud(session: Session, apply_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    site = await load_site_terms(
        session,
        _gateway(handler),
        company_name="Acme",
        apply_url=apply_url,
        named_url="",
        posting="Apply on LinkedIn only.",
        now=SOON,
    )

    assert site.terms == []
    assert "No employer website" in site.note


async def test_a_later_url_is_used_when_the_first_title_does_not_name_the_company(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "other.example":
            page = b"<html><head><title>Other Holdings</title></head><body>Other</body></html>"
            return httpx.Response(200, headers={"content-type": "text/html"}, content=page)
        if request.url.host == "acme.example":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_PAGE)
        return httpx.Response(404)

    site = await load_site_terms(
        session,
        _gateway(handler),
        company_name="Acme",
        apply_url="https://other.example/jobs/1",
        named_url="https://acme.example/",
        posting="",
        now=SOON,
    )

    assert "Payments" in site.terms
