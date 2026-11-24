"""The one worker process: ``python -m app.worker`` runs until stopped, ``--once`` runs one cycle.

What is due comes from the database, not from a clock time, so a machine that was
asleep or off catches up the next time this runs.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.careers import CareersReport, collect_careers
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.fetch.gateway import FetchGateway
from app.google_jobs import GoogleJobsReport, collect_google_jobs
from app.indeed import IndeedReport, collect_indeed
from app.interview.service import process_requests
from app.linkedin import LinkedinReport, collect_linkedin
from app.matching import semantic
from app.matching.judge import judge_jobs
from app.matching.match import match_all
from app.matching.report import write_reports
from app.quality import store_results
from app.skills.taxonomy import map_job_skills
from app.summaries.generate import summarise_matches
from app.tailor import write_tailored_cvs

# Searches run once a day; the short poll keeps AI work on the backlog moving.
DEFAULT_POLL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class CycleReport:
    linkedin: LinkedinReport
    indeed: IndeedReport
    google_jobs: GoogleJobsReport
    careers: CareersReport


async def run_cycle(
    session: Session,
    gateway: FetchGateway,
    *,
    persist: Callable[[], None] | None = None,
) -> CycleReport:
    save = persist or session.flush
    settings = get_settings()

    def clock() -> datetime:
        return datetime.now(UTC)

    linkedin = await collect_linkedin(session, gateway=gateway, settings=settings, persist=save, now=clock)
    indeed = await collect_indeed(session, gateway=gateway, settings=settings, persist=save, now=clock)
    google = await collect_google_jobs(session, gateway=gateway, settings=settings, persist=save, now=clock)
    # D69: jobs on employers' own sites, found through Google, that LinkedIn and Indeed may not have.
    careers = await collect_careers(session, gateway=gateway, settings=settings, persist=save, now=clock)
    collected = (linkedin, indeed, google, careers)
    if any(report.searches or report.closed for report in collected):
        now = datetime.now(UTC)
        store_results(session, now=now)
        map_job_skills(session)
        match_all(session, now=now)
        save()
    # Every cycle, not only after collection: a backlog stopped by a cap carries on next time (D50).
    judged = await judge_jobs(session, gateway=gateway, settings=settings, persist=save)
    if judged.judged:
        match_all(session, now=datetime.now(UTC), applicant_ids=judged.judged)
        save()
    # D52: the full report for jobs the judge scored 3.0 or more; its score can then take a job off the page.
    reported = await write_reports(session, gateway=gateway, settings=settings, persist=save)
    if reported.written:
        match_all(session, now=datetime.now(UTC), applicant_ids=reported.written)
        save()
    # D55: tailor a CV for Apply / Consider reports; budget waits leave packages pending.
    await write_tailored_cvs(session, gateway=gateway, settings=get_settings(), persist=save)
    # Every cycle, not only after collection: a backlog stopped by the day's token cap carries on tomorrow.
    await summarise_matches(session, gateway=gateway, settings=settings, persist=save)
    # After summaries, so a job's vector is made from its summary when it has one (D48).
    semantic.embed_matches(session, semantic.local_embedder())
    save()
    await process_requests(session, gateway=gateway, settings=get_settings(), persist=save)
    save()
    return CycleReport(linkedin=linkedin, indeed=indeed, google_jobs=google, careers=careers)


async def _main(once: bool, poll_seconds: int) -> None:
    gateway = FetchGateway()
    try:
        while True:
            try:
                with SessionLocal() as session:
                    report = await run_cycle(session, gateway, persist=session.commit)
            except SQLAlchemyError as exc:
                # A database restart must not silently end daily collection.
                if once:
                    raise
                detail = getattr(exc, "orig", None) or exc
                print(f"cycle failed, will retry: {type(exc).__name__}: {detail}", flush=True)
            else:
                linkedin, indeed, google, careers = report.linkedin, report.indeed, report.google_jobs, report.careers
                print(
                    f"linkedin searches {linkedin.searches}, jobs {linkedin.jobs}, new {linkedin.postings_new}, "
                    f"closed {linkedin.closed}, failed {linkedin.failed}, stopped by {linkedin.stopped_by}; "
                    f"indeed searches {indeed.searches}, jobs {indeed.jobs}, new {indeed.postings_new}, "
                    f"closed {indeed.closed}, failed {indeed.failed}, stopped by {indeed.stopped_by}; "
                    f"google_jobs searches {google.searches}, jobs {google.jobs}, new {google.postings_new}, "
                    f"closed {google.closed}, failed {google.failed}, stopped by {google.stopped_by}; "
                    f"company sites searches {careers.searches}, google pages {careers.google_pages}, "
                    f"sites opened {careers.sites_opened}, jobs {careers.jobs}, new {careers.postings_new}, "
                    f"closed {careers.closed}, skipped {careers.skipped}, failed {careers.failed}, "
                    f"stopped by {careers.stopped_by}",
                    flush=True,
                )
            if once:
                return
            await asyncio.sleep(poll_seconds)
    finally:
        await gateway.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one cycle and stop")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.once, args.poll_seconds))
    except KeyboardInterrupt:
        print("worker stopped")


if __name__ == "__main__":
    main()
