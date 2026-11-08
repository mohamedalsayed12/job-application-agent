"""``python -m app.cleaning``: replay stored fetch runs, then print job counts and completeness."""

from __future__ import annotations

from datetime import UTC, datetime

from app.cleaning.clean import (
    completeness,
    link_method_counts,
    master_job_counts,
    reclean_all,
)
from app.core.db import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        report = reclean_all(session, now=datetime.now(UTC))
        session.commit()
        print(
            f"postings new {report.postings_new}, updated {report.postings_updated}, "
            f"unusable {report.unusable}, closed {report.freshness.closed}, "
            f"reopened {report.freshness.reopened}, unknown {report.freshness.unknown}"
        )
        print(f"jobs by freshness: {master_job_counts(session)}")
        print(f"links by method: {link_method_counts(session)}")
        for source, fields in completeness(session).items():
            print(f"{source}: {fields}")


if __name__ == "__main__":
    main()
