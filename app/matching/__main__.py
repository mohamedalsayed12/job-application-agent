"""``python -m app.matching``: match stored jobs to every applicant and print the best for each."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import SessionLocal
from app.matching.match import match_all
from app.models import ApplicantJobMatch, ApplicantPreference, Job

TOP = 15


def main() -> None:
    with SessionLocal() as session:
        report = match_all(session, now=datetime.now(UTC))
        session.commit()
        print(f"applicants {report.applicants}, matches {report.matches}, removed {report.removed}")
        for preference in session.scalars(select(ApplicantPreference)).all():
            rows = session.execute(
                select(ApplicantJobMatch, Job)
                .join(Job, Job.id == ApplicantJobMatch.job_id)
                .where(ApplicantJobMatch.applicant_id == preference.applicant_id)
                .order_by(ApplicantJobMatch.score.desc(), Job.company_name, Job.title)
            ).all()
            fitting = [row for row in rows if row[0].location_fit]
            print(
                f"\n{preference.target_roles} in {preference.locations}: "
                f"{len(rows)} role matches, {len(fitting)} also fit the location"
            )
            for match, job in fitting[:TOP]:
                role = match.reasons["role"]
                print(f"  {match.score} {job.company_name}: {job.title} [{role['kind']}, {match.reasons['location']}]")
                print(f"      {job.apply_url}")


if __name__ == "__main__":
    main()
