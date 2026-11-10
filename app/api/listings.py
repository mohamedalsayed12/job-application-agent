"""Stage 13: the logged-in person's matched jobs, best first (T36, T37, D42).

Reading this page never calls the AI. A summary is written when someone opens a job, so a job
nobody has opened yet shows its description here.
"""

from __future__ import annotations

import html
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.accounts import require_login
from app.api.admin_html import page
from app.api.chrome import applicant_bar
from app.cleaning.linking import is_primary
from app.core.db import get_db
from app.matching import semantic
from app.matching.match import MAX_SCORE
from app.matching.semantic import Embedder, closeness, vectors_for
from app.models import (
    Applicant,
    ApplicantJobMatch,
    ApplicantPreference,
    ApplicantSkill,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
    JobSummary,
    SummaryStatus,
)

router = APIRouter(prefix="/listings", tags=["listings"])

PAGE_SIZE = 25
_CURRENCY = {"GBP": "£", "USD": "$", "EUR": "€"}
_PERIOD = {"year": "a year", "month": "a month", "week": "a week", "day": "a day", "hour": "an hour"}
_SOURCE_LABEL = {"greenhouse": "Employer's Greenhouse board", "lever": "Employer's Lever board"}
Sort = Literal["score", "newest"]
# D44: matches on a target role, matches on skills alone, or both.
Show = Literal["roles", "skills", "all"]
_SKILLS_KIND = "skills"

_STYLE = """<style>
.filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: 0.75rem; align-items: end; margin-bottom: 1.25rem; }
.filters label { margin: 0; }
.filters .search { grid-column: 1 / -1; }
.salary-note { font-size: 0.85rem; color: var(--muted); }
.job-head { display: flex; justify-content: space-between; gap: 1rem; }
.job h2 { margin: 0; font-size: 1.1rem; }
.job h2 a { color: var(--text); text-decoration: none; }
.job h2 a:hover { text-decoration: underline; }
.company { margin: 0.15rem 0; font-size: 0.9rem; color: var(--muted); }
.company b { color: var(--text); }
.score { text-align: right; min-width: 6.5rem; }
.score .label { font-size: 0.75rem; color: var(--muted); display: block; }
.score b { font-size: 1.8rem; color: var(--primary); line-height: 1.1; }
.score small { font-size: 0.8rem; }
.bar { height: 4px; background: var(--primary-soft); border-radius: 2px; margin-top: 0.3rem; }
.bar i { display: block; height: 100%; background: var(--primary); border-radius: 2px; }
.about { font-size: 0.9rem; margin: 0.6rem 0; }
.about details { color: var(--muted); }
.about .full { white-space: pre-line; color: var(--text); margin-top: 0.4rem; max-height: 22rem; overflow: auto; }
.skills-row { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; font-size: 0.8rem; }
.why { background: var(--bg); border-radius: 8px; padding: 0.5rem 0.75rem; margin-top: 0.75rem; font-size: 0.85rem; }
.why summary { cursor: pointer; font-weight: 600; }
.why ul { margin: 0.5rem 0 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 0.25rem 1rem; }
.job-foot { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; margin-top: 0.75rem; font-size: 0.8rem; color: var(--muted); }
.discover { background: var(--primary-soft); border-color: #c9d5f7; font-size: 0.9rem; }
.pager { display: flex; justify-content: space-between; align-items: center; font-size: 0.9rem; }
@media (max-width: 34rem) { .job-head { flex-direction: column; } .score { text-align: left; } }
</style>"""


@dataclass(frozen=True, slots=True)
class Listing:
    job_id: uuid.UUID
    title: str
    company: str
    places: str
    salary: str
    summary: str | None
    description: str | None
    score: Decimal
    reasons: dict[str, Any]
    matched_skills: list[str]
    missing_skills: list[str]
    missing_licences: list[str]
    last_seen_at: datetime | None
    source: str
    apply_url: str


def salary_text(job: Job) -> str:
    if not job.salary_known or (job.salary_min is None and job.salary_max is None):
        return "Not stated"
    symbol = _CURRENCY.get(job.salary_currency or "", f"{job.salary_currency or ''} ")
    amounts = [f"{symbol}{amount:,.0f}" for amount in (job.salary_min, job.salary_max) if amount is not None]
    span = " to ".join(dict.fromkeys(amounts))
    period = _PERIOD.get(job.salary_period or "", "")
    return f"{span} {period}".strip()


_REGION = {"emea": "EMEA", "apac": "APAC", "americas": "Americas", "europe": "Europe", "worldwide": "Worldwide"}


def place_names(job: Job) -> str:
    names = [
        place.get("city") or place.get("country") or _REGION.get(place.get("region") or "", place.get("region") or "")
        for place in job.places or []
    ]
    text = ", ".join(dict.fromkeys(name for name in names if name)) or "Not stated"
    return f"{text} (remote)" if job.remote_type == "remote" else text


def score_out_of_100(score: Decimal) -> int:
    return min(100, int((score / MAX_SCORE * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _sources(session: Session, jobs: list[Job]) -> dict[uuid.UUID, str]:
    by_job: dict[uuid.UUID, list[JobPosting]] = defaultdict(list)
    for job_id, posting in session.execute(
        select(JobLink.job_id, JobPosting)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .where(JobLink.job_id.in_([job.id for job in jobs]))
    ).all():
        by_job[job_id].append(posting)
    sources = {}
    for job in jobs:
        postings = by_job.get(job.id, [])
        primary = next((posting for posting in postings if is_primary(job, posting)), postings[0] if postings else None)
        sources[job.id] = primary.source if primary else "unknown"
    return sources


def load_listings(
    session: Session,
    applicant_id: uuid.UUID,
    *,
    everywhere: bool,
    page_number: int,
    role: str = "",
    sort: Sort = "score",
    show: Show = "roles",
    search: str = "",
    embedder: Embedder | None = None,
) -> tuple[list[Listing], int]:
    """One page of open matched jobs, best score (or newest) first, and how many there are in all.

    With a search, the same jobs come closest in meaning first (D48).
    """
    query = (
        select(ApplicantJobMatch, Job)
        .join(Job, Job.id == ApplicantJobMatch.job_id)
        .where(ApplicantJobMatch.applicant_id == applicant_id, Job.freshness == JobFreshness.open)
    )
    if not everywhere:
        query = query.where(ApplicantJobMatch.location_fit.is_(True))
    query = _only(query, show)
    if role and show != "skills":
        query = query.where(ApplicantJobMatch.reasons["role"]["target"].astext == role)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    first = (Job.first_seen_at.desc(), ApplicantJobMatch.score.desc()) if sort == "newest" else (ApplicantJobMatch.score.desc(),)
    query = query.order_by(*first, Job.company_name, Job.title, Job.id)
    start = (page_number - 1) * PAGE_SIZE
    if search and embedder is not None:
        everything = session.execute(query).all()
        close = closeness(search, vectors_for(session, [job for _, job in everything], embedder), embedder)
        # sorted() keeps the score order among equally close jobs.
        rows = sorted(everything, key=lambda row: -close[row[1].id])[start : start + PAGE_SIZE]
    else:
        rows = session.execute(query.limit(PAGE_SIZE).offset(start)).all()
    jobs = [job for _, job in rows]
    summaries = dict(
        session.execute(
            select(JobSummary.job_id, JobSummary.summary_text).where(
                JobSummary.job_id.in_([job.id for job in jobs]), JobSummary.status == SummaryStatus.accepted
            )
        ).all()
    )
    sources = _sources(session, jobs) if jobs else {}
    listings = [
        Listing(
            job_id=job.id,
            title=job.title,
            company=job.company_name,
            places=place_names(job),
            salary=salary_text(job),
            summary=summaries.get(job.id),
            description=job.description,
            score=match.score,
            reasons=match.reasons,
            matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
            missing_licences=match.missing_licences,
            last_seen_at=job.last_seen_at,
            source=sources[job.id],
            apply_url=job.apply_url,
        )
        for match, job in rows
    ]
    return listings, total


def _only(query, show: Show):
    kind = ApplicantJobMatch.reasons["role"]["kind"].astext
    if show == "roles":
        return query.where(kind != _SKILLS_KIND)
    if show == "skills":
        return query.where(kind == _SKILLS_KIND)
    return query


def count_skill_matches(session: Session, applicant_id: uuid.UUID, *, everywhere: bool) -> int:
    query = (
        select(func.count())
        .select_from(ApplicantJobMatch)
        .join(Job, Job.id == ApplicantJobMatch.job_id)
        .where(
            ApplicantJobMatch.applicant_id == applicant_id,
            Job.freshness == JobFreshness.open,
            ApplicantJobMatch.reasons["role"]["kind"].astext == _SKILLS_KIND,
        )
    )
    if not everywhere:
        query = query.where(ApplicantJobMatch.location_fit.is_(True))
    return session.scalar(query) or 0


def _e(value: object) -> str:
    return html.escape(str(value))


def _apply(url: str) -> str:
    if not url.lower().startswith(("https://", "http://")):
        return '<span class="muted">No usable apply link was given.</span>'
    return f'<a class="button primary" href="{html.escape(url, quote=True)}" rel="noopener noreferrer">Apply on the original posting</a>'


def _reasons(item: Listing) -> str:
    role = item.reasons.get("role", {})
    level = f", {role['level']}" if role.get("level") else ""
    if role.get("kind") == _SKILLS_KIND:
        named = len(item.matched_skills) + len(item.missing_skills)
        role_line = f"Role: not one of your target roles, but you have {len(item.matched_skills)} of the {named} skills it names{level}"
    else:
        role_line = f"Role: {role.get('kind', 'unknown')} match for {role.get('target', '')}{level}"
    lines = [
        role_line,
        f"Location: {item.reasons.get('location', 'unknown')}",
        f"Salary: {item.reasons.get('salary', 'unknown')}",
        f"Skills you have: {', '.join(item.matched_skills) or 'none named'}",
        f"Skills you don't list: {', '.join(item.missing_skills) or 'none'}",
    ]
    if item.missing_licences:
        lines.append(f"Licences you don't list: {', '.join(item.missing_licences)}")
    return "<ul>" + "".join(f"<li>{_e(line)}</li>" for line in lines) + "</ul>"


def _skills(item: Listing) -> str:
    if not (item.matched_skills or item.missing_skills or item.missing_licences):
        return ""
    chips = [f'<span class="tag blue" title="On your profile">{_e(name)}</span>' for name in item.matched_skills]
    chips += [f'<span class="tag miss" title="Not on your profile">{_e(name)}</span>' for name in item.missing_skills]
    chips += [f'<span class="tag bad" title="Licence you don\'t list">{_e(name)}</span>' for name in item.missing_licences]
    return f'<div class="skills-row"><span class="muted">Skills in the advert</span>{"".join(chips)}</div>'


_SKILLS_TAG = '<span class="tag blue">Fits your skills</span>'


def render_listing(item: Listing) -> str:
    if item.summary:
        about = f"<p>{_e(item.summary)}</p><p><small>AI summary, checked against the posting.</small></p>"
    else:
        about = (
            f'<details><summary>Description</summary><div class="full">'
            f"{_e(item.description or 'No description was given.')}</div></details>"
        )
    seen = item.last_seen_at.strftime("%d %b %Y") if item.last_seen_at else "not recorded"
    out_of_100 = score_out_of_100(item.score)
    return f"""
<article class="card job">
<div class="job-head">
  <div>
    <h2><a href="/jobs/{item.job_id}">{_e(item.title)}</a></h2>
    <p class="company"><b>{_e(item.company)}</b> · {_e(item.places)}</p>
    <div class="tags">
      <span class="tag">Salary: {_e(item.salary)}</span>
      {_SKILLS_TAG if item.reasons.get("role", {}).get("kind") == _SKILLS_KIND else ""}
      <span class="tag good">Open · confirmed {_e(seen)}</span>
      <span class="tag">{_e(_SOURCE_LABEL.get(item.source, item.source))}</span>
    </div>
  </div>
  <div class="score" title="Raw score {_e(item.score)} of {_e(MAX_SCORE)}">
    <span class="label">Match score</span><b>{out_of_100}</b><small>/100</small>
    <div class="bar"><i style="width: {out_of_100}%"></i></div>
  </div>
</div>
<div class="about">{about}</div>
{_skills(item)}
<details class="why"><summary>Why this score?</summary>{_reasons(item)}</details>
<div class="job-foot">
  <span>Last confirmed open {_e(seen)}. Source: {_e(item.source)}.</span>
  {_apply(item.apply_url)}
</div>
</article>
"""


def _query(**params: object) -> str:
    return html.escape(urlencode({key: value for key, value in params.items() if value not in ("", None, False, "score", "roles")}))


def _option(value: str, label: str, selected: str) -> str:
    return f'<option value="{_e(value)}"{" selected" if value == selected else ""}>{_e(label)}</option>'


def _order(search: str, sort: Sort) -> str:
    if search:
        return f"Closest in meaning to &ldquo;{_e(search)}&rdquo; first. <a href=\"/listings\">Clear search</a>"
    return f"Sorted by {'newest' if sort == 'newest' else 'match score'}."


def _filters(
    preference: ApplicantPreference | None, *, role: str, everywhere: bool, sort: Sort, show: Show, search: str = ""
) -> str:
    roles = "".join(_option(name, name, role) for name in (preference.target_roles if preference else []))
    if preference and preference.salary_amount is not None:
        symbol = _CURRENCY.get(preference.salary_currency or "", f"{preference.salary_currency or ''} ")
        period = _PERIOD.get(preference.salary_period or "", "")
        salary = f"Your minimum salary: <b>{_e(f'{symbol}{preference.salary_amount:,.0f} {period}'.strip())}</b>"
    else:
        salary = "No minimum salary set"
    return f"""
<form class="filters" method="get" action="/listings">
  <label class="search">Search your matches<input type="search" name="q" value="{_e(search)}" maxlength="200"
    placeholder="e.g. python backend fintech, data work with SQL"></label>
  <label>Show<select name="show" onchange="this.form.submit()">
    {_option("roles", "My target roles", show)}{_option("skills", "Roles my skills fit", show)}{_option("all", "Both", show)}
  </select></label>
  <label>Role<select name="role" onchange="this.form.submit()">{_option("", "All my target roles", role)}{roles}</select></label>
  <label>Location<select name="everywhere" onchange="this.form.submit()">
    {_option("0", "My locations", "1" if everywhere else "0")}{_option("1", "All locations", "1" if everywhere else "0")}
  </select></label>
  <label>Sort by<select name="sort" onchange="this.form.submit()">
    {_option("score", "Match score", sort)}{_option("newest", "Newest first", sort)}
  </select></label>
  <div><noscript><button type="submit">Apply filters</button></noscript>
    <p class="salary-note">{salary} · <a href="/profile">Edit</a></p></div>
</form>"""


def _nav(page_number: int, total: int, *, everywhere: bool, role: str, sort: Sort, show: Show, search: str = "") -> str:
    pages = max(1, -(-total // PAGE_SIZE))
    previous = (
        f'<a href="/listings?{_query(page=page_number - 1, everywhere=int(everywhere), role=role, sort=sort, show=show, q=search)}">← Previous</a>'
        if page_number > 1
        else "<span></span>"
    )
    following = (
        f'<a href="/listings?{_query(page=page_number + 1, everywhere=int(everywhere), role=role, sort=sort, show=show, q=search)}">Next →</a>'
        if page_number < pages
        else "<span></span>"
    )
    return f'<div class="pager">{previous}<span class="muted">Page {page_number} of {pages}</span>{following}</div>'


def _discover_hint(
    db: Session, applicant: Applicant, preference: ApplicantPreference | None, *, everywhere: bool, sort: Sort
) -> str:
    """Points people to jobs their skills fit that their target roles missed (D44)."""
    extra = count_skill_matches(db, applicant.id, everywhere=everywhere)
    if extra:
        link = f"/listings?{_query(everywhere=int(everywhere), sort=sort, show='skills')}"
        many = extra != 1
        return (
            f'<div class="card discover"><b>{extra} more job{"s" if many else ""} fit{"" if many else "s"} your skills</b>, '
            f'though {"they aren&#39;t roles" if many else "it isn&#39;t a role"} you listed. You might qualify for more than '
            f'you think. <a href="{link}">See {"them" if many else "it"}</a></div>'
        )
    has_skills = db.scalar(select(func.count()).select_from(ApplicantSkill).where(ApplicantSkill.applicant_id == applicant.id))
    if preference is not None and not has_skills:
        return (
            '<div class="card discover">Add your skills on <a href="/profile">your profile</a>, or let us read them from '
            "your CV, to discover roles your skills fit that you might not have searched for.</div>"
        )
    return ""


def search_embedder() -> Callable[[], Embedder]:
    """Hands over the loader, not the model, so a page without a search never loads it."""
    return semantic.local_embedder


@router.get("", response_class=HTMLResponse)
def listings_page(
    request: Request,
    applicant: Applicant = Depends(require_login),
    db: Session = Depends(get_db),
    page_number: int = Query(1, alias="page", ge=1),
    everywhere: bool = Query(False),
    role: str = Query("", max_length=200),
    sort: Sort = Query("score"),
    show: Show = Query("roles"),
    q: str = Query("", max_length=200),
    embedder: Callable[[], Embedder] = Depends(search_embedder),
) -> HTMLResponse:
    search = " ".join(q.split())
    listings, total = load_listings(
        db,
        applicant.id,
        everywhere=everywhere,
        page_number=page_number,
        role=role,
        sort=sort,
        show=show,
        search=search,
        embedder=embedder() if search else None,
    )
    if search:
        db.commit()
    preference = db.scalar(select(ApplicantPreference).where(ApplicantPreference.applicant_id == applicant.id))
    where = "in every location" if everywhere else "in your locations"
    hint = _discover_hint(db, applicant, preference, everywhere=everywhere, sort=sort) if show == "roles" else ""
    if listings:
        body = "".join(render_listing(item) for item in listings)
    elif preference is None:
        body = '<div class="card"><p>Fill in <a href="/profile">your profile</a> and we\'ll find your matches.</p></div>'
    else:
        body = '<div class="card"><p>No open jobs match these filters yet. Matching runs after each collection.</p></div>'
    return page(
        "Your matches",
        f"""{_STYLE}
<div class="page-head"><div>
  <h1>Your matches</h1>
  <p class="muted">{total} open jobs {where}. {_order(search, sort)}</p>
</div></div>
{_filters(preference, role=role, everywhere=everywhere, sort=sort, show=show, search=search)}
{hint}
{body}
{_nav(page_number, total, everywhere=everywhere, role=role, sort=sort, show=show, search=search)}""",
        header=applicant_bar(request, "matches"),
    )
