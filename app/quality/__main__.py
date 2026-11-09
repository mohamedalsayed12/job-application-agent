"""``python -m app.quality``: check every job, store the results and print where we stand."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.db import SessionLocal
from app.quality.report import JobRow, build_report, store_results


def _line(row: JobRow) -> str:
    return (
        f"{row.label}: jobs {row.jobs}, live {row.live}, recent {row.recent_pct}%, real high {row.real_high_pct}%, "
        f"complete {row.complete_pct}%, unique {row.unique_pct}%, duplicates {row.duplicate_pct}%, "
        f"expired {row.expired_pct}%, last success {row.last_success}"
    )


def main() -> None:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        checked = store_results(session, now=now)
        session.commit()
        report = build_report(session, now=now)
    jobs = report.jobs
    print(f"checked {checked} jobs")
    for target in jobs.targets:
        print(f"{target.name}: {target.value} against {target.goal}: {'met' if target.met else 'MISSED'}")
    for row in (jobs.total, *jobs.by_source, *jobs.by_sector):
        print(_line(row))
    print(f"duplicates in the same city {jobs.same_city_duplicates}, older than 60 days {jobs.older_than_60_days}")
    print(f"real {jobs.real_levels}, missing {jobs.missing_counts}, not recent {jobs.not_recent_counts}")
    for gap in report.gaps:
        print(f"note: {gap}")


if __name__ == "__main__":
    main()
