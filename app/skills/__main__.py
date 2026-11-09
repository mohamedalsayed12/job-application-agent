"""Load the skills list, map every job's skills, and print ESCO's coverage (A12)."""

from __future__ import annotations

from app.core.db import SessionLocal
from app.skills.taxonomy import coverage_report, load_taxonomy, map_job_skills


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def main() -> None:
    with SessionLocal() as session:
        loaded = load_taxonomy(session)
        stored = map_job_skills(session)
        session.commit()
        report = coverage_report(session)
    print(f"Skills on the list: {loaded}")
    print(f"Job skills stored: {stored}")
    print(f"Open jobs naming a known skill: {report.jobs_with_skills} of {report.open_jobs}")
    print(f"Mentions in open jobs: {report.mentions}")
    print(f"  in ESCO: {report.esco_mentions} ({_share(report.esco_mentions, report.mentions)})")
    print(f"  on our own list: {report.local_mentions} ({_share(report.local_mentions, report.mentions)})")
    print("Skills on neither list can't be counted, so ESCO's real share is at most this figure.")
    print("Most common:")
    for label, source, count in report.most_common:
        print(f"  {count:5}  {source:5}  {label}")


if __name__ == "__main__":
    main()
