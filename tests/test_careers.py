"""D69: Google search through SerpAPI finds jobs on employers' own sites, not LinkedIn or Indeed."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.careers import (
    Wanted,
    canonical_url,
    collect_careers,
    copied_from_linkedin_or_indeed,
    employer_site,
    google_query,
    is_job_board,
    job_fit,
    job_links,
    read_page,
    wanted_by_search,
)
from app.cleaning.normalize import normalize_job
from app.core.config import Settings
from app.fetch.gateway import FetchGateway
from app.linkedin import Search
from app.models import (
    ApplicantSkill,
    BudgetPolicy,
    FetchOutcome,
    FetchRun,
    JobPosting,
    SpendLedger,
)
from app.skills.taxonomy import label_index, load_taxonomy, skill_for_text
from tests.jobs import DAY_1, _public_resolve
from tests.test_matching import _applicant


def _posting(
    *,
    title: str = "Graduate Backend Developer",
    company: str = "Acme",
    company_url: str | None = "https://www.acme.example",
    description: str = "Build APIs in Python with a small team.",
    posted: str = "2026-11-05",
    **extra: Any,
) -> dict[str, Any]:
    org: dict[str, Any] = {"@type": "Organization", "name": company}
    if company_url:
        org["sameAs"] = company_url
    return {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": f"<p>{description}</p>",
        "datePosted": posted,
        "employmentType": "FULL_TIME",
        "hiringOrganization": org,
        "jobLocation": {"@type": "Place", "address": {"addressLocality": "Manchester", "addressCountry": "GB"}},
        **extra,
    }


def _page(*postings: dict[str, Any], links: str = "") -> str:
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(p)}</script>' for p in postings)
    return f"<html><head>{blocks}</head><body>{links}</body></html>"


class _Web:
    """SerpAPI answers with ``google`` pages in turn; every other https host serves ``pages``."""

    def __init__(self, google: list[list[str]], pages: dict[str, str]) -> None:
        self.google, self.pages = google, pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.host == "serpapi.com":
            start = int(parse_qs(request.url.query.decode())["start"][0])
            index = start // 10
            links = self.google[index] if index < len(self.google) else []
            if not links:
                return httpx.Response(200, json={"error": "Google hasn't returned any results for this query."})
            return httpx.Response(200, json={"organic_results": [{"link": link} for link in links]})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="missing", headers={"content-type": "text/html"})

    def gateway(self) -> FetchGateway:
        return FetchGateway(
            user_agent="JobApplicationAgent-Test/0.1",
            allowlist=frozenset({"serpapi.com"}),
            resolve=_public_resolve,
            transport=httpx.MockTransport(self),
        )

    def hosts(self) -> set[str]:
        return {request.url.host for request in self.requests}

    def google_calls(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == "serpapi.com"]


def _policy(session: Session, money_cap: str = "1") -> None:
    session.add(
        BudgetPolicy(
            provider="*",
            period="month",
            money_cap=Decimal(money_cap),
            currency="GBP",
            effective_from=DAY_1 - timedelta(days=30),
        )
    )
    session.flush()


async def _collect(session: Session, web: _Web, *, applicant_id: object, at=DAY_1, **settings: object):
    gateway = web.gateway()
    try:
        opts = {"serpapi_api_key": "test-key", "ai_applicant_ids": str(applicant_id), **settings}
        return await collect_careers(
            session,
            gateway=gateway,
            settings=Settings(**opts),  # type: ignore[arg-type]
            persist=session.flush,
            now=lambda: at,
        )
    finally:
        await gateway.aclose()


# --- The checks, one at a time -----------------------------------------------------------


def test_the_google_query_names_the_role_at_entry_level_and_leaves_out_linkedin_and_indeed() -> None:
    query = google_query(Search("Junior Backend Developer", "Manchester"))
    # Not in quotes, so Google also returns similar titles.
    assert query.startswith('Backend Developer (graduate OR junior OR "entry level") Manchester careers')
    assert "-site:linkedin.com" in query
    assert "-site:indeed.com" in query
    assert "-site:indeed.co.uk" in query


def test_job_boards_are_recognised_by_host() -> None:
    assert is_job_board("https://uk.linkedin.com/jobs/view/1")
    assert is_job_board("https://uk.indeed.com/viewjob?jk=1")
    assert is_job_board("https://www.reed.co.uk/jobs/1")
    assert not is_job_board("https://careers.acme.example/jobs/1")


def test_a_job_that_leads_back_to_linkedin_or_indeed_counts_as_copied() -> None:
    page = "https://acme.example/jobs/1"
    assert not copied_from_linkedin_or_indeed(_posting(), page)
    # The employer's own LinkedIn company page is normal and does not count.
    assert not copied_from_linkedin_or_indeed(_posting(company_url="https://www.linkedin.com/company/acme"), page)
    assert copied_from_linkedin_or_indeed(_posting(url="https://www.linkedin.com/jobs/view/123"), page)
    assert copied_from_linkedin_or_indeed(_posting(url="https://uk.indeed.com/viewjob?jk=abc"), page)
    assert copied_from_linkedin_or_indeed(_posting(description="Originally posted on Indeed."), page)
    assert copied_from_linkedin_or_indeed(_posting(description="Apply via LinkedIn today"), page)
    assert copied_from_linkedin_or_indeed(_posting(), "https://uk.indeed.com/viewjob?jk=abc")


def test_a_job_must_be_on_the_employers_own_site_or_its_tracking_system() -> None:
    assert employer_site(_posting(), "https://careers.acme.example/jobs/1")
    assert employer_site(_posting(company_url=None), "https://acme.example/jobs/1")
    assert employer_site(_posting(company="Beta Labs Ltd", company_url=None), "https://boards.greenhouse.io/beta/1")
    assert not employer_site(_posting(company="Beta Labs", company_url=None), "https://www.reed.co.uk/jobs/1")
    assert not employer_site(_posting(company="Beta Labs", company_url=None), "https://jobs-aggregator.example/1")


def test_the_same_or_a_similar_title_fits_the_role() -> None:
    backend = Wanted("Backend Developer")
    advert = "Build APIs."
    assert job_fit("Graduate Back-end Developer (Python)", advert, backend) == "exact"
    assert job_fit("Junior Backend Engineer", advert, Wanted("Graduate Backend Developer")) == "exact"
    assert job_fit("Graduate Software Engineer", advert, backend) == "similar"
    assert job_fit("Junior Python Engineer", advert, backend) == "similar"
    assert job_fit("Graduate Full Stack Developer", advert, backend) == "similar"
    assert job_fit("Graduate Data Analyst", advert, backend) is None
    assert job_fit("Sales Engineer", advert, backend) is None
    assert job_fit("Backend Developer Internship", advert, backend) is None


def _skilled(session: Session, skills: tuple[str, ...]) -> object:
    load_taxonomy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    index = label_index(session)
    session.add_all(
        ApplicantSkill(applicant_id=applicant_id, text_as_typed=typed, skill_id=skill_for_text(index, typed))
        for typed in skills
    )
    session.flush()
    return applicant_id


def test_a_different_title_fits_when_the_advert_asks_for_the_persons_skillset(session: Session) -> None:
    applicant_id = _skilled(session, ("Python", "Postgres", "Docker"))
    wanted = wanted_by_search(session, {applicant_id})[("backend developer", "manchester")]

    same_skills = "You will write Python, store data in Postgres and ship it with Docker."
    assert job_fit("Graduate Data Engineer", same_skills, wanted) == "skills"
    # Two of the person's skills is not enough, and nor is half or less of what the advert asks for.
    assert job_fit("Graduate Data Engineer", "Python and Docker.", wanted) is None
    assert job_fit("Graduate Data Engineer", "Python, Postgres, Docker, Java, React, TypeScript and AWS.", wanted) is None
    # Without the skillset, the title alone decides.
    assert job_fit("Graduate Data Engineer", same_skills, Wanted("Backend Developer")) is None


def test_jobs_are_read_from_json_ld_including_graphs() -> None:
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, _posting()]}
    html = f'<script type="application/ld+json">{json.dumps(graph)}</script><a href="/x">X</a>'
    postings, links = read_page(html)
    assert [p["title"] for p in postings] == ["Graduate Backend Developer"]
    assert links == [("/x", "X")]


def test_only_same_site_links_that_name_the_role_are_followed() -> None:
    links = [
        ("/jobs/graduate-backend-developer", ""),
        ("https://jobs.acme.example/role/2", "Junior Backend Developer"),
        ("/jobs/sales-executive", "Sales Executive"),
        ("https://other.example/jobs/backend-developer", "Backend Developer"),
    ]
    assert job_links(links, "https://acme.example/careers", "Backend Developer") == [
        "https://acme.example/jobs/graduate-backend-developer",
        "https://jobs.acme.example/role/2",
    ]


def test_tracking_parameters_do_not_make_a_new_job() -> None:
    assert canonical_url("https://Acme.example/jobs/1?utm_source=google&gh_jid=7#apply") == (
        "https://acme.example/jobs/1?gh_jid=7"
    )


def test_a_company_job_page_becomes_a_normal_job() -> None:
    posting = _posting(
        baseSalary={
            "@type": "MonetaryAmount",
            "currency": "GBP",
            "value": {"@type": "QuantitativeValue", "minValue": 28000, "maxValue": 32000, "unitText": "YEAR"},
        },
        validThrough="2026-12-01T00:00:00Z",
    )
    job = normalize_job("careers", {"page_url": "https://acme.example/jobs/1", "posting": posting})
    assert job is not None
    assert (job.title, job.company, job.apply_url) == (
        "Graduate Backend Developer",
        "Acme",
        "https://acme.example/jobs/1",
    )
    assert (job.location.city, job.location.country) == ("Manchester", "GB")
    assert (job.salary.minimum, job.salary.maximum, job.salary.period) == (Decimal(28000), Decimal(32000), "year")
    assert job.hours == "full_time"
    assert job.description == "Build APIs in Python with a small team."


# --- The whole search, as a person would do it ------------------------------------------


async def test_google_results_are_opened_and_only_fitting_employer_jobs_are_kept(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    web = _Web(
        google=[
            [
                "https://www.linkedin.com/jobs/view/1",  # never opened
                "https://www.reed.co.uk/jobs/2",  # job board, never opened
                "https://acme.example/jobs/grad-backend",  # a job: kept
                "https://beta.example/careers",  # a careers list: its matching link is followed
                "https://copycat.example/jobs/3",  # a job copied from Indeed: not kept
                "https://gamma.example/jobs/4",  # a different role: not kept
            ]
        ],
        pages={
            "https://acme.example/jobs/grad-backend": _page(_posting()),
            "https://beta.example/careers": _page(
                links='<a href="/jobs/7">Junior Backend Developer</a><a href="/jobs/8">Office Manager</a>'
            ),
            "https://beta.example/jobs/7": _page(
                _posting(title="Junior Backend Developer", company="Beta", company_url="https://beta.example")
            ),
            "https://copycat.example/jobs/3": _page(
                _posting(company="Copycat", company_url=None, url="https://uk.indeed.com/viewjob?jk=9")
            ),
            "https://gamma.example/jobs/4": _page(
                _posting(title="Graduate Data Analyst", company="Gamma", company_url=None)
            ),
        },
    )

    report = await _collect(session, web, applicant_id=applicant_id)

    assert (report.searches, report.jobs, report.postings_new, report.failed) == (1, 2, 2, [])
    assert report.skipped == {
        "job board": 2,
        "copied from LinkedIn or Indeed": 1,
        "not the role or its skillset": 1,
    }
    assert "www.linkedin.com" not in web.hosts()
    assert "www.reed.co.uk" not in web.hosts()
    assert "beta.example" in web.hosts()
    postings = {row.source_job_id: row for row in session.scalars(select(JobPosting))}
    assert set(postings) == {"https://acme.example/jobs/grad-backend", "https://beta.example/jobs/7"}
    assert {row.source for row in postings.values()} == {"careers"}
    assert postings["https://beta.example/jobs/7"].apply_url == "https://beta.example/jobs/7"
    call = web.google_calls()[0]
    assert parse_qs(call.url.query.decode())["api_key"] == ["test-key"]
    assert "-site:indeed.com" in parse_qs(call.url.query.decode())["q"][0]
    spent = list(session.scalars(select(SpendLedger)))
    assert [(row.kind, row.provider, row.actual_cost) for row in spent] == [
        ("careers_search", "serpapi", Decimal("0.0200"))
    ]


async def test_too_few_jobs_on_one_google_page_means_reading_the_next(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    first = [f"https://nojob{n}.example/" for n in range(10)]
    web = _Web(
        google=[first, ["https://acme.example/jobs/1"]],
        pages={"https://acme.example/jobs/1": _page(_posting())},
    )

    # Set here so a CAREERS_MAX_PAGES in the developer's .env cannot change the test.
    report = await _collect(session, web, applicant_id=applicant_id, careers_max_pages=3)

    assert [int(parse_qs(r.url.query.decode())["start"][0]) for r in web.google_calls()] == [0, 10]
    assert (report.google_pages, report.jobs) == (2, 1)


async def test_once_enough_jobs_are_found_no_more_google_pages_are_read(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    web = _Web(
        google=[["https://acme.example/jobs/1", "https://acme.example/jobs/2"], ["https://acme.example/jobs/3"]],
        pages={
            "https://acme.example/jobs/1": _page(_posting()),
            "https://acme.example/jobs/2": _page(_posting()),
        },
    )

    report = await _collect(session, web, applicant_id=applicant_id, careers_enough_jobs=1)

    assert (report.google_pages, report.jobs) == (1, 1)
    assert len(web.google_calls()) == 1
    assert "https://acme.example/jobs/2" not in {str(r.url) for r in web.requests}


async def test_a_closed_or_old_job_is_not_kept(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    web = _Web(
        google=[["https://acme.example/jobs/1", "https://acme.example/jobs/2"]],
        pages={
            "https://acme.example/jobs/1": _page(_posting(validThrough="2026-11-01T00:00:00Z")),
            "https://acme.example/jobs/2": _page(_posting(posted="2026-08-01")),
        },
    )
    report = await _collect(session, web, applicant_id=applicant_id)
    assert report.jobs == 0
    assert report.skipped == {"closed": 1, "too old": 1}


async def test_searches_run_once_a_day(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    await _collect(session, _Web([[]], {}), applicant_id=applicant_id)

    later = _Web([[]], {})
    report = await _collect(session, later, applicant_id=applicant_id, at=DAY_1 + timedelta(hours=23))
    assert (report.searches, later.requests) == (0, [])


async def test_without_a_serpapi_key_nothing_is_searched(session: Session) -> None:
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    web = _Web([["https://acme.example/jobs/1"]], {})
    report = await _collect(session, web, applicant_id=applicant_id, serpapi_api_key="")
    assert (report.stopped_by, web.requests) == ("not_configured", [])


async def test_with_no_budget_left_google_is_not_searched(session: Session) -> None:
    _policy(session, money_cap="0.001")
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    web = _Web([["https://acme.example/jobs/1"]], {})
    report = await _collect(session, web, applicant_id=applicant_id)
    assert (report.stopped_by, web.requests) == ("budget", [])


async def test_a_refused_search_is_recorded_as_failed_and_costs_nothing(session: Session) -> None:
    _policy(session)
    applicant_id = _applicant(session, roles=["Backend Developer"], locations=["Manchester"])

    class _Refusing(_Web):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(401, json={"error": "Invalid API key."})

    report = await _collect(session, _Refusing([], {}), applicant_id=applicant_id)

    assert report.failed == ["Backend Developer in Manchester: SerpAPI answered 401"]
    run = session.scalars(select(FetchRun)).one()
    assert (run.source, run.outcome) == ("careers", FetchOutcome.failed)
    assert session.scalars(select(SpendLedger)).one().actual_cost == Decimal(0)


async def test_a_company_job_with_a_different_title_but_the_same_skillset_is_kept(session: Session) -> None:
    _policy(session)
    applicant_id = _skilled(session, ("Python", "Postgres", "Docker"))
    web = _Web(
        google=[["https://acme.example/jobs/1", "https://acme.example/jobs/2"]],
        pages={
            "https://acme.example/jobs/1": _page(
                _posting(
                    title="Graduate Data Engineer",
                    description="You will write Python, store data in Postgres and ship it with Docker.",
                )
            ),
            "https://acme.example/jobs/2": _page(
                _posting(title="Graduate Data Engineer", description="Spreadsheets and reporting.")
            ),
        },
    )

    report = await _collect(session, web, applicant_id=applicant_id)

    assert report.jobs == 1
    assert report.skipped == {"not the role or its skillset": 1}
    raw = session.scalar(select(JobPosting).where(JobPosting.source_job_id == "https://acme.example/jobs/1"))
    assert raw is not None
