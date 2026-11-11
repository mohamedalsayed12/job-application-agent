"""Admin quality report page (MVP section 16). Reading it changes nothing."""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.admin_auth import require_admin
from app.api.admin_html import page
from app.api.chrome import admin_bar
from app.core.db import get_db
from app.quality.report import Example, JobRow, QualityReport, Target, build_report

router = APIRouter(prefix="/admin/quality", tags=["admin-quality"])


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]], *, empty: str = "None.") -> str:
    rows = list(rows)
    if not rows:
        return f"<p>{html.escape(empty)}</p>"
    head = "".join(f"<th>{html.escape(name)}</th>" for name in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_text(cell))}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


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


_STYLE = """<style>
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr)); gap: 0.75rem; margin: 0.5rem 0 1rem; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 0.8rem 0.9rem; }
.stat .name { font-size: 0.8rem; color: var(--muted); }
.stat .value { font-size: 1.5rem; font-weight: 700; margin: 0.15rem 0; }
.stat .goal { font-size: 0.75rem; color: var(--muted); }
.queue { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
</style>"""


def _stat(name: str, value: object, note: str = "", tag: str = "") -> str:
    return (
        f'<div class="stat"><div class="name">{html.escape(name)}</div>'
        f'<div class="value">{html.escape(_text(value))}</div>'
        f'<div class="goal">{html.escape(note)} {tag}</div></div>'
    )


def _targets(targets: Iterable[Target]) -> str:
    return '<div class="stats">' + "".join(
        _stat(
            target.name,
            target.value,
            f"Target {target.goal}",
            '<span class="tag good">Met</span>' if target.met else '<span class="tag bad">Missed</span>',
        )
        for target in targets
    ) + "</div>"


def _render(report: QualityReport) -> str:
    jobs, spend, collection, relevance = report.jobs, report.spend, report.collection, report.relevance
    return f"""
{_STYLE}
<div class="page-head"><div>
  <h1>Quality report</h1>
  <p class="muted">Worked out at {html.escape(_text(report.generated_at))} UTC. Reading this page changes nothing.</p>
</div></div>
<h2>Targets</h2>
{_targets(jobs.targets)}
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
<h2>Spending</h2>
<div class="stats">
  {_stat("Spent this month", f"£{spend.this_month:.2f}")}
  {_stat("Spent in total", f"£{spend.total:.2f}")}
</div>
{_table(("Kind", "This month £"), ((kind, f"{amount:.4f}") for kind, amount in spend.by_kind_this_month.items()), empty="Nothing spent this month.")}
{_table(("Provider", "Period", "Money cap", "Request cap", "Currency"), spend.caps, empty="No budget caps set.")}
<h2>Collection</h2>
{_table(("Source", "Outcomes"), ((source, ", ".join(f"{name} {count}" for name, count in sorted(found.items()))) for source, found in sorted(collection.outcomes_by_source.items())), empty="No searches yet.")}
<h3>Jobs by freshness</h3>
{_counts(collection.jobs_by_freshness)}
<h2>Summaries</h2>
<p>Written: {report.summaries.accepted}. Rejected by the check: {report.summaries.rejected}. Being written: {report.summaries.in_progress}. AI cost: £{report.summaries.cost:.4f}.</p>
<h2 id="relevance">Relevance</h2>
<p>How well jobs fit each applicant. This is not one of the four quality measures above, so poor matching can be told apart from poor data.</p>
{_table(("Measure", "Now"), (
    ("Applicants with a match", f"{relevance.applicants_with_matches} of {relevance.applicants}"),
    ("Matches", relevance.matches),
    ("Matches in a wanted location", relevance.fitting_location),
    ("Matches whose advert names skills", relevance.naming_skills),
    ("Share of those skills the applicant has", "none" if relevance.skill_share_pct is None else f"{relevance.skill_share_pct}%"),
    ("Matches missing a required licence", relevance.missing_licences),
))}
<h2>What this report cannot show yet</h2>
<ul>{"".join(f"<li>{html.escape(gap)}</li>" for gap in report.gaps)}</ul>
"""


@router.get("")
def quality_report(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return page("Quality report", _render(build_report(db)), header=admin_bar(request, "quality"))
