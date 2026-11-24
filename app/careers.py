"""D69: jobs on employers' own websites, found the way a person would look for them.

A person who wants jobs that are not on LinkedIn or Indeed types the job title into Google,
opens the company sites that come up, reads each job to see whether it fits, and searches
further when it does not. Once a day this does the same for each target role and location:

1. One Google search through SerpAPI, with LinkedIn, Indeed and the big job boards left out.
2. Each result that is not a job board is opened on the company's own site, through the
   fetch gateway, so robots.txt, the rate limit and the domain checks all still apply.
3. A page counts as a job only when it carries schema.org ``JobPosting`` data. A careers
   page that only lists jobs is followed into a few of its links that look like the role.
4. A job is kept only when it fits: the same or a similar title (a Python Engineer for a
   Backend Developer), or a different title that asks for the person's skillset (the D44
   skills rule). It must also be still open and recent, sit on the employer's own site or
   applicant-tracking system, and nothing in it may point back to LinkedIn or Indeed (the
   check that it was not copied from them).
5. While a search has found too few jobs, the next page of Google results is read, up to a limit.

Each Google page is one SerpAPI search, reserved against the monthly money cap first.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.cleaning.clean import clean_run, close_stale
from app.cleaning.normalize import _iso_date
from app.core.config import Settings
from app.fetch.due import due_searches
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse
from app.fetch.policy import host_matches_suffix
from app.indeed import _core_role
from app.linkedin import Search, searches
from app.matching.match import SkillFit, match_by_skills, match_role, worth_judging
from app.matching.titles import clean_html
from app.models import (
    ApplicantPreference,
    ApplicantSkill,
    FetchOutcome,
    FetchRun,
    RawPayload,
    SpendKind,
)
from app.skills.mapping import LabelEntry, find_in_text
from app.skills.taxonomy import label_index

SOURCE = "careers"
SERPAPI = "https://serpapi.com/search.json"
RESULTS_PER_PAGE = 10

# The two sources this search exists to go beyond. A job that leads back to either is not kept.
LINKEDIN_OR_INDEED = ("linkedin.com", "lnkd.in", "indeed.com", "indeed.co.uk", "indeedjobs.com")
# Job boards and aggregators: they are not the employer's site and often repost LinkedIn or Indeed jobs.
JOB_BOARDS = (
    *LINKEDIN_OR_INDEED,
    "glassdoor.com", "glassdoor.co.uk", "reed.co.uk", "totaljobs.com", "cv-library.co.uk",
    "cwjobs.co.uk", "jobsite.co.uk", "monster.com", "monster.co.uk", "ziprecruiter.com",
    "ziprecruiter.co.uk", "adzuna.co.uk", "adzuna.com", "jooble.org", "simplyhired.com",
    "simplyhired.co.uk", "careerjet.co.uk", "talent.com", "jobrapido.com", "whatjobs.com",
    "jobted.co.uk", "technojobs.co.uk", "gradcracker.com", "prospects.ac.uk", "targetjobs.co.uk",
    "milkround.com", "ratemyplacement.co.uk", "brightnetwork.co.uk", "welcometothejungle.com",
    "otta.com", "builtin.com", "jobs.ac.uk", "findajob.dwp.gov.uk", "google.com", "google.co.uk",
    "facebook.com", "x.com", "twitter.com", "reddit.com", "youtube.com", "wikipedia.org",
)
# Left out of the Google query itself; Google allows about 32 words, so only the biggest ones.
_QUERY_EXCLUSIONS = (
    "linkedin.com", "indeed.com", "indeed.co.uk", "glassdoor.co.uk", "reed.co.uk", "totaljobs.com",
    "cv-library.co.uk",
)
# Applicant-tracking systems employers use for their own careers pages.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com", "smartrecruiters.com", "teamtailor.com",
    "recruitee.com", "personio.de", "personio.com", "bamboohr.com", "myworkdayjobs.com", "icims.com",
    "jobvite.com", "breezy.hr", "pinpointhq.com", "applytojob.com", "tal.net", "hirehive.com",
    "successfactors.com", "successfactors.eu", "taleo.net",
)
_SECOND_LEVEL = frozenset({"co", "org", "ac", "gov", "ltd", "plc", "me", "net", "com"})
_COMPANY_FILLER = frozenset(
    {"the", "ltd", "limited", "plc", "llp", "inc", "group", "holdings", "uk", "co", "company", "and", "of"}
)
# Wording a copied job carries: links to LinkedIn or Indeed job pages, or "posted on" / "apply on" them.
_COPIED = re.compile(
    r"linkedin\.com/jobs|lnkd\.in/|indeed\.(?:com|co\.uk)/|"
    r"\b(?:originally\s+)?(?:posted|listed|found|advertised)\s+(?:on|via)\s+(?:linkedin|indeed)\b|"
    r"\bapply\s+(?:on|via|through)\s+(?:linkedin|indeed)\b|\bsource:\s*(?:linkedin|indeed)\b",
    re.IGNORECASE,
)
_TRACKING = re.compile(r"^(?:utm_[a-z]+|gclid|fbclid|msclkid|ref|source|src)$", re.IGNORECASE)


@dataclass(slots=True)
class CareersReport:
    searches: int = 0
    google_pages: int = 0
    sites_opened: int = 0
    jobs: int = 0
    postings_new: int = 0
    closed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def google_query(search: Search) -> str:
    """What a person would type: the role, entry level, the place, 'careers', minus the big boards.

    The role is not in quotes, so Google also returns similar titles, as it would for a person.
    """
    left_out = " ".join(f"-site:{site}" for site in _QUERY_EXCLUSIONS)
    return f'{core_role(search.role)} (graduate OR junior OR "entry level") {search.location} careers {left_out}'


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _on(host: str, suffixes: tuple[str, ...]) -> bool:
    return bool(host) and any(host_matches_suffix(host, suffix) for suffix in suffixes)


def is_job_board(url: str) -> bool:
    return _on(_host(url), JOB_BOARDS)


def site_of(host: str) -> str:
    """The registrable part of a host: careers.acme.co.uk -> acme.co.uk, jobs.acme.com -> acme.com."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonical_url(url: str) -> str:
    """The same job reached with tracking parameters or a fragment is still the same job."""
    parts = urlsplit(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if not _TRACKING.match(key)])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", query, ""))


def _words(text: str) -> list[str]:
    # "Back-end" and "backend" are the same word on a careers page.
    return re.findall(r"[a-z0-9+#]+", text.lower().replace("-", ""))


def core_role(role: str) -> str:
    """The role without its level words: the level is checked later, when jobs are matched (D46)."""
    cleaned = " ".join(role.split())
    return _core_role(cleaned) or cleaned


@dataclass(frozen=True, slots=True)
class Wanted:
    """What the people behind one search want: their role, and the skills on their profiles."""

    role: str
    skills: frozenset[uuid.UUID] = frozenset()
    labels: dict[str, LabelEntry] = field(default_factory=dict)


def job_fit(title: str, description: str | None, wanted: Wanted) -> str | None:
    """How a job fits: "exact" or "similar" title, or "skills" for a different title with the same skillset.

    The same rules matching uses, so a job kept here is one matching can show. None when it does not fit.
    """
    found = match_role(title, description, [core_role(wanted.role)])
    if found is not None:
        return found.kind.value
    if not wanted.skills or not wanted.labels:
        return None
    named = {hit.skill_id for hit in find_in_text(description or "", wanted.labels)}
    fit = SkillFit(
        matched=[str(skill) for skill in named & wanted.skills],
        missing=[str(skill) for skill in named - wanted.skills],
        on_profile=True,
    )
    found = match_by_skills(title, description, fit, level=None)
    return found.kind.value if found is not None else None


def link_looks_like_role(text: str, role: str) -> bool:
    """A careers-page link worth opening: it fits the role or shares a word with it, and is not a different job."""
    core = core_role(role)
    return match_role(text, None, [core]) is not None or worth_judging(text, [core])


def wanted_by_search(session: Session, applicant_ids: set[uuid.UUID] | None) -> dict[tuple[str, str], Wanted]:
    """Each role and location, with every skill held by anyone who wants it."""
    labels = label_index(session)
    query = select(ApplicantPreference)
    if applicant_ids is not None:
        query = query.where(ApplicantPreference.applicant_id.in_(applicant_ids))
    held: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for applicant_id, skill_id in session.execute(
        select(ApplicantSkill.applicant_id, ApplicantSkill.skill_id).where(ApplicantSkill.skill_id.is_not(None))
    ):
        held[applicant_id].add(skill_id)
    skills: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
    for preference in session.scalars(query):
        for role in preference.target_roles:
            for location in preference.locations or ["United Kingdom"]:
                skills[(role.strip().lower(), location.strip().lower())] |= held[preference.applicant_id]
    return {key: Wanted(key[0], frozenset(found), labels) for key, found in skills.items()}


def copied_from_linkedin_or_indeed(posting: dict[str, Any], page_url: str) -> bool:
    """True when the job's page, links or wording lead back to LinkedIn or Indeed.

    The employer's own company profile (``hiringOrganization.sameAs``) may be a LinkedIn
    company page; that is normal and is not counted. Everything else in the job is.
    """
    if _on(_host(page_url), LINKEDIN_OR_INDEED):
        return True
    rest = {key: value for key, value in posting.items() if key != "hiringOrganization"}
    for url in _urls(rest):
        if _on(_host(url), LINKEDIN_OR_INDEED):
            return True
    return bool(_COPIED.search(json.dumps(rest, ensure_ascii=False)))


def _urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        return [url for item in value.values() for url in _urls(item)]
    if isinstance(value, list):
        return [url for item in value for url in _urls(item)]
    return []


def employer_site(posting: dict[str, Any], page_url: str) -> bool:
    """The job is on the hiring company's own site, or on the tracking system it uses."""
    host = _host(page_url)
    if _on(host, ATS_HOSTS):
        return True
    if _on(host, JOB_BOARDS):
        return False
    org = posting.get("hiringOrganization")
    name = org.get("name") if isinstance(org, dict) else org
    if isinstance(org, dict):
        for url in _urls([org.get("url"), org.get("sameAs")]):
            if not _on(_host(url), JOB_BOARDS) and site_of(_host(url)) == site_of(host):
                return True
    return _name_in_host(name if isinstance(name, str) else "", host)


def _name_in_host(name: str, host: str) -> bool:
    tokens = [word for word in _words(name) if word not in _COMPANY_FILLER]
    label = site_of(host).split(".")[0].replace("-", "") if host else ""
    if not tokens or len(label) < 3:
        return False
    joined = "".join(tokens)
    return joined in label or label in joined or any(len(word) >= 4 and word in label for word in tokens)


class _Page(HTMLParser):
    """The JSON-LD blocks and the links of one HTML page. Standard library only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._script: list[str] | None = None
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").strip().lower() == "application/ld+json":
            self._script = []
        elif tag == "a" and values.get("href"):
            self._href, self._text = values["href"], []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self._href is not None:
            self._text.append(data)


def read_page(html: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """The schema.org JobPostings on a page, and its links as (href, text)."""
    page = _Page()
    page.feed(html)
    page.close()
    postings: list[dict[str, Any]] = []
    for block in page.json_ld:
        try:
            postings.extend(_job_postings(json.loads(block)))
        except ValueError:
            continue
    return postings, page.links


def _job_postings(data: object, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(data, list):
        return [posting for item in data for posting in _job_postings(item, depth + 1)]
    if not isinstance(data, dict):
        return []
    kind = data.get("@type")
    if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
        return [data]
    return _job_postings(data.get("@graph"), depth + 1)


def job_links(links: list[tuple[str, str]], page_url: str, role: str) -> list[str]:
    """Links on a careers page, on the same site, whose text (or path) looks like the role."""
    site = site_of(_host(page_url))
    found: dict[str, str] = {}
    for href, text in links:
        url = urljoin(page_url, href)
        if not url.startswith("https://") or site_of(_host(url)) != site:
            continue
        label = text or re.sub(r"[-_/]+", " ", urlsplit(url).path)
        if link_looks_like_role(label, role):
            found.setdefault(canonical_url(url), url)
    return list(found.values())


def _rejection(
    posting: dict[str, Any], page_url: str, wanted: Wanted, *, now: datetime, max_age_days: int
) -> str | None:
    """Why a job on a company page is not kept, or None when it is."""
    if copied_from_linkedin_or_indeed(posting, page_url):
        return "copied from LinkedIn or Indeed"
    if not employer_site(posting, page_url):
        return "not the employer's own site"
    description = clean_html(_text(posting.get("description")))
    if not description:
        return "no description"
    if job_fit(clean_html(_text(posting.get("title"))), description, wanted) is None:
        return "not the role or its skillset"
    closes = _iso_date(posting.get("validThrough"))
    if closes is not None and closes <= now:
        return "closed"
    posted = _iso_date(posting.get("datePosted"))
    if posted is not None and posted < now - timedelta(days=max_age_days):
        return "too old"
    return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


async def collect_careers(
    session: Session,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime],
) -> CareersReport:
    report = CareersReport()
    key = settings.serpapi_api_key.strip()
    if not key:
        report.stopped_by = "not_configured"
        return report
    gate = BudgetGate(session, clock=now)
    skipped: Counter[str] = Counter()
    applicants = settings.ai_applicants()
    wanted = wanted_by_search(session, applicants)
    due = due_searches(
        session,
        SOURCE,
        searches(session, applicants),
        now=now(),
        every_hours=settings.careers_every_hours,
        settings=settings,
    )
    if not due:
        return report
    for item in due:
        search = item.search
        started = now()
        hunt = _Hunt(gateway, key, settings, now, wanted.get((search.role.lower(), search.location.lower())))
        await hunt.run(search, gate, persist)
        if hunt.google_pages == 0 and hunt.out_of_budget:
            report.stopped_by = "budget"
            break
        report.searches += 1
        report.google_pages += hunt.google_pages
        report.sites_opened += hunt.visits
        skipped.update(hunt.skipped)
        ok = hunt.problem is None
        if not ok:
            report.failed.append(f"{search.role} in {search.location}: {hunt.problem}")
        stored = store_items(
            session, hunt.items, started_at=started, finished_at=now(), ok=ok,
            requests=hunt.google_pages + hunt.visits, search_key=item.key,
        )
        report.jobs += len(hunt.items)
        report.postings_new += stored
        persist()
        if hunt.out_of_budget:
            report.stopped_by = "budget"
            break
    report.skipped = dict(skipped)
    report.closed = close_stale(session, source=SOURCE, now=now(), after=timedelta(days=settings.careers_stale_days))
    persist()
    return report


def store_items(
    session: Session,
    items: list[dict[str, Any]],
    *,
    started_at: datetime,
    finished_at: datetime,
    ok: bool = True,
    requests: int = 1,
    search_key: str | None = None,
) -> int:
    """Keep one search's jobs as raw payloads and clean them into jobs. Returns how many postings were new."""
    rows = [item for item in items if isinstance(item, dict) and job_id(item)]
    outcome = FetchOutcome.failed if not ok else FetchOutcome.complete if rows else FetchOutcome.empty
    run = FetchRun(
        source=SOURCE,
        outcome=outcome,
        jobs_returned=len(rows),
        request_count=max(requests, 1),
        started_at=started_at,
        finished_at=finished_at,
        search_key=search_key,
    )
    session.add(run)
    session.flush()
    for item in rows:
        body = json.dumps(item, sort_keys=True, ensure_ascii=False)
        session.add(
            RawPayload(
                fetch_run_id=run.id,
                source=SOURCE,
                source_job_id=job_id(item),
                payload=item,
                payload_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                fetched_at=finished_at,
            )
        )
    session.flush()
    return clean_run(session, run).postings_new


def job_id(item: dict[str, Any]) -> str | None:
    """The employer's page address is the job's identity on its own site."""
    url = item.get("page_url")
    return canonical_url(url) if isinstance(url, str) and url.startswith("https://") else None


class _Hunt:
    """One role and location: search Google, open the company pages, keep the jobs that fit."""

    def __init__(
        self,
        gateway: FetchGateway,
        key: str,
        settings: Settings,
        now: Callable[[], datetime],
        wanted: Wanted | None,
    ) -> None:
        self.gateway, self.key, self.settings, self.now = gateway, key, settings, now
        self.wanted = wanted
        self.items: list[dict[str, Any]] = []
        self.skipped: Counter[str] = Counter()
        self.seen: set[str] = set()
        self.google_pages = 0
        self.visits = 0
        self.out_of_budget = False
        self.problem: str | None = None

    def _enough(self) -> bool:
        return len(self.items) >= self.settings.careers_enough_jobs or self.visits >= self.settings.careers_max_visits

    async def run(self, search: Search, gate: BudgetGate, persist: Callable[[], None]) -> None:
        query = google_query(search)
        for page in range(self.settings.careers_max_pages):
            if self._enough():
                return
            price = self.settings.careers_search_price
            try:
                reservation = gate.reserve(
                    SpendEstimate(
                        provider="serpapi",
                        kind=SpendKind.careers_search,
                        units=Decimal(1),
                        unit_label="search",
                        estimated_cost=price,
                    )
                )
            except BudgetWait:
                self.out_of_budget = True
                return
            persist()
            results = await self._google(query, start=page * RESULTS_PER_PAGE)
            # SerpAPI does not charge for a search that errors, so only an answered search costs.
            gate.settle(reservation, actual_cost=price if results is not None else Decimal(0))
            self.google_pages += 1
            if results is None:
                # A later page failing keeps what earlier pages found; a first page failing is a failed search.
                if page > 0:
                    self.problem = None
                return
            for link in results:
                if self._enough():
                    return
                await self._open(link, search, follow=True)
            if len(results) < RESULTS_PER_PAGE:
                return  # Google has no more pages for this search

    async def _google(self, query: str, *, start: int) -> list[str] | None:
        params = {
            "engine": "google",
            "q": query,
            "google_domain": "google.co.uk",
            "gl": "uk",
            "hl": "en",
            "start": start,
            "num": RESULTS_PER_PAGE,
            "api_key": self.key,
        }
        response = await self.gateway.fetch(
            FetchRequest(url=f"{SERPAPI}?{urlencode(params)}", kind="api", timeout_s=30)
        )
        if isinstance(response, FetchRefusal):
            self.problem = f"SerpAPI not reached ({response.reason})"
            return None
        try:
            data = json.loads(response.body)
        except ValueError:
            data = None
        if response.status_code >= 400 or not isinstance(data, dict):
            # SerpAPI says "no results" as an error with a 200; anything else is a real failure.
            self.problem = f"SerpAPI answered {response.status_code}"
            return None
        error = data.get("error")
        if isinstance(error, str) and error:
            if "hasn't returned any results" in error:
                return []
            self.problem = "SerpAPI refused the search"
            return None
        organic = data.get("organic_results")
        if not isinstance(organic, list):
            return []
        return [item["link"] for item in organic if isinstance(item, dict) and isinstance(item.get("link"), str)]

    async def _open(self, url: str, search: Search, *, follow: bool) -> None:
        if not url.startswith("https://"):
            self.skipped["not https"] += 1
            return
        if is_job_board(url):
            self.skipped["job board"] += 1
            return
        key = canonical_url(url)
        if key in self.seen:
            return
        self.seen.add(key)
        self.visits += 1
        page = await self.gateway.fetch(
            FetchRequest(url=url, kind="page", official_domain=site_of(_host(url)), timeout_s=15)
        )
        if not isinstance(page, FetchResponse) or page.status_code != 200 or page.content_type != "text/html":
            self.skipped["could not open"] += 1
            return
        postings, links = read_page(page.body.decode("utf-8", errors="replace"))
        if postings:
            self._keep(postings, page.url, search, found_from=url)
            return
        if not follow:
            self.skipped["no job on page"] += 1
            return
        # A careers page that lists jobs: open the ones whose link names the role, as a person would.
        for link in job_links(links, page.url, search.role)[: self.settings.careers_links_per_site]:
            if self._enough():
                return
            await self._open(link, search, follow=False)

    def _keep(self, postings: list[dict[str, Any]], page_url: str, search: Search, *, found_from: str) -> None:
        wanted = self.wanted or Wanted(search.role)
        reasons = []
        for posting in postings:
            reason = _rejection(
                posting, page_url, wanted, now=self.now(), max_age_days=self.settings.careers_max_age_days
            )
            if reason is None:
                fit = job_fit(
                    clean_html(_text(posting.get("title"))), clean_html(_text(posting.get("description"))), wanted
                )
                self.items.append(
                    {
                        "page_url": page_url,
                        "found_from": found_from,
                        "query": google_query(search),
                        "fit": fit,
                        "posting": posting,
                    }
                )
                return  # one job per page: the page address is the job's identity
            reasons.append(reason)
        self.skipped[reasons[0]] += 1
