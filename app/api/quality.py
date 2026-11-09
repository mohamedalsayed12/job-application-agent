"""Admin quality report page (MVP section 16). Reading it changes nothing."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin_key
from app.api.admin_html import page
from app.core.db import get_db
from app.quality.report import Example, JobRow, QualityReport, build_report

router = APIRouter(prefix="/admin/quality", tags=["admin-quality"])


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]], *, empty: str = "None.") -> str:
    rows = list(rows)
    if not rows:
        return f"<p>{html.escape(empty)}</p>"
    head = "".join(f"<th>{html.escape(name)}</th>" for name in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_text(cell))}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _text(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _counts(counts: dict[str, int]) -> str:
    return _table(("Value", "Count"), sorted(counts.items()))


def _job_rows(rows: Iterable[JobRow]) -> str:
    return _table(
        ("Group", "Jobs", "Live", "Recent %", "Real high %", "Complete %", "Unique %", "Duplicates %", "Expired %", "Last successful run"),
        (
            (
                row.label,
                row.jobs,
                row.live,
                row.recent_pct,
                row.real_high_pct,
                row.complete_pct,
                row.unique_pct,
                row.duplicate_pct,
                row.expired_pct,
                row.last_success,
            )
            for row in rows
        ),
        empty="No jobs yet.",
    )


def _examples(heading: str, examples: list[Example]) -> str:
    if not examples:
        return ""
    return f"<h3>{html.escape(heading)}</h3>" + _table(
        ("Company", "Title", "Detail"), ((item.company, item.title, item.detail) for item in examples)
    )


def _render(report: QualityReport) -> str:
    jobs, discovery, collection, audit = report.jobs, report.discovery, report.collection, report.audit
    backlog_age = "" if discovery.oldest_review_days is None else f", oldest {discovery.oldest_review_days} days"
    return f"""
<h1>Quality report</h1>
<p>Worked out at {html.escape(_text(report.generated_at))} UTC.</p>
<h2>Targets</h2>
{_table(("Measure", "Target", "Now", "Result"), ((t.name, t.goal, t.value, "Met" if t.met else "Missed") for t in jobs.targets))}
<h2>Jobs</h2>
<p>Percentages are of live jobs, which are all jobs except closed ones. Expired % is of all jobs.</p>
{_job_rows([jobs.total])}
<h3>By source</h3>
{_job_rows(jobs.by_source)}
<h3>By sector</h3>
{_job_rows(jobs.by_sector)}
<p>Same advert in other places, kept as separate jobs: {jobs.same_advert_elsewhere}. Older than 60 days, flagged and kept: {jobs.older_than_60_days}.</p>
<h3>Real confidence</h3>
{_counts(jobs.real_levels)}
<h3>Missing for Complete</h3>
{_counts(jobs.missing_counts)}
<h3>Why jobs are not Recent</h3>
{_counts(jobs.not_recent_counts)}
{_examples("Incomplete jobs", jobs.incomplete_examples)}
{_examples("Duplicate jobs", jobs.duplicate_examples)}
{_examples("Jobs below high confidence", jobs.lower_confidence_examples)}
<h2>Discovery</h2>
{_table(("Template", "Searches", "Cost £"), discovery.searches_by_template, empty="No searches yet.")}
<p>Candidates found: {discovery.candidates}.</p>
{_counts(discovery.candidates_by_state)}
<p>Review backlog: {discovery.review_backlog}{backlog_age}.
AI ungrounded rate: {html.escape(_text(discovery.ungrounded_pct))}%.
Verified boards: {discovery.boards}. Spent in total: £{discovery.spend_total:.4f}, including AI work not tied to one search.
Verified boards per £: {html.escape(_text(discovery.boards_per_pound))}.
Spent this month: £{discovery.spend_this_month:.4f}.</p>
{_table(("Provider", "Period", "Money cap", "Request cap", "Currency"), discovery.caps, empty="No budget caps set.")}
<h2>Collection</h2>
{_table(("Source", "Outcomes"), ((source, ", ".join(f"{name} {count}" for name, count in sorted(found.items()))) for source, found in sorted(collection.outcomes_by_source.items())), empty="No fetches yet.")}
<h3>Boards with repeated failures</h3>
{_table(("Company", "Failures in a row"), collection.repeated_failures)}
<h3>Safety-brake events</h3>
{_table(("Company", "Fetch finished", "Jobs gone", "Jobs before"), collection.brake_events)}
<h3>Jobs by freshness</h3>
{_counts(collection.jobs_by_freshness)}
<h2>Audit</h2>
<p>Latest audit day: {html.escape(_text(audit.latest_day))}. Rows: {audit.sampled}.
Correct: {audit.correct} of {audit.judged} judged. Useful: {audit.useful} of {audit.useful_judged} judged.</p>
<h2>What this report cannot show yet</h2>
<ul>{"".join(f"<li>{html.escape(gap)}</li>" for gap in report.gaps)}</ul>
"""


@router.get("")
def quality_report(
    _admin_key: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    return page("Quality report", _render(build_report(db)))
