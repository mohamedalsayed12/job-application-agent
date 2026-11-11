"""The one worker process: ``python -m app.worker`` runs until stopped, ``--once`` runs one cycle.

What is due comes from the database, not from a clock time, so a machine that was
asleep or off catches up the next time this runs.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.fetch.gateway import FetchGateway
from app.linkedin import LinkedinReport, collect_linkedin
from app.matching import semantic
from app.matching.judge import judge_jobs
from app.matching.match import match_all
from app.matching.report import write_reports
from app.quality import store_results
from app.skills.taxonomy import map_job_skills
from app.summaries.generate import summarise_matches

# Searches run once a day; the short poll keeps AI work on the backlog moving.
DEFAULT_POLL_SECONDS = 60


async def run_cycle(
    session: Session,
    gateway: FetchGateway,
    *,
    persist: Callable[[], None] | None = None,
) -> LinkedinReport:
    save = persist or session.flush
    linkedin = await collect_linkedin(
        session, gateway=gateway, settings=get_settings(), persist=save, now=lambda: datetime.now(UTC)
    )
    if linkedin.searches or linkedin.closed:
        now = datetime.now(UTC)
        store_results(session, now=now)
        map_job_skills(session)
        match_all(session, now=now)
        save()
    # Every cycle, not only after collection: a backlog stopped by a cap carries on next time (D50).
    judged = await judge_jobs(session, gateway=gateway, settings=get_settings(), persist=save)
    if judged.judged:
        match_all(session, now=datetime.now(UTC), applicant_ids=judged.judged)
        save()
    # D52: the full report for jobs the judge scored 3.0 or more; its score can then take a job off the page.
    reported = await write_reports(session, gateway=gateway, settings=get_settings(), persist=save)
    if reported.written:
        match_all(session, now=datetime.now(UTC), applicant_ids=reported.written)
        save()
    # Every cycle, not only after collection: a backlog stopped by the day's token cap carries on tomorrow.
    await summarise_matches(session, gateway=gateway, settings=get_settings(), persist=save)
    # After summaries, so a job's vector is made from its summary when it has one (D48).
    semantic.embed_matches(session, semantic.local_embedder())
    save()
    return linkedin


async def _main(once: bool, poll_seconds: int) -> None:
    gateway = FetchGateway()
    try:
        while True:
            try:
                with SessionLocal() as session:
                    linkedin = await run_cycle(session, gateway, persist=session.commit)
            except SQLAlchemyError as exc:
                # A database restart must not silently end daily collection.
                if once:
                    raise
                print(f"cycle failed, will retry: {type(exc).__name__}", flush=True)
            else:
                print(
                    f"linkedin searches {linkedin.searches}, jobs {linkedin.jobs}, new {linkedin.postings_new}, "
                    f"closed {linkedin.closed}, failed {linkedin.failed}, stopped by {linkedin.stopped_by}",
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
