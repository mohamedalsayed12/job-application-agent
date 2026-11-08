"""The one worker process: ``python -m app.worker`` runs until stopped, ``--once`` runs one cycle.

What is due comes from the database, not from a clock time, so a machine that was
asleep or off catches up the next time this runs.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collection import (
    CollectionReport,
    RegistrationReport,
    collect_due,
    register_verified_boards,
)
from app.core.db import SessionLocal
from app.fetch.gateway import FetchGateway

# Short enough for the first retry, one minute after a failed read, to happen on time.
DEFAULT_POLL_SECONDS = 60


async def run_cycle(
    session: Session,
    gateway: FetchGateway,
    *,
    persist: Callable[[], None] | None = None,
) -> tuple[RegistrationReport, CollectionReport]:
    save = persist or session.flush
    registration = register_verified_boards(session)
    save()
    collection = await collect_due(session, gateway, persist=save)
    return registration, collection


async def _main(once: bool, poll_seconds: int) -> None:
    gateway = FetchGateway()
    try:
        while True:
            try:
                with SessionLocal() as session:
                    registration, collection = await run_cycle(session, gateway, persist=session.commit)
            except SQLAlchemyError as exc:
                # A database restart must not silently end daily collection.
                if once:
                    raise
                print(f"cycle failed, will retry: {type(exc).__name__}", flush=True)
            else:
                print(
                    f"registered {len(registration.registered)}, skipped {registration.skipped}, "
                    f"fetches {dict(collection.outcomes)}, jobs {collection.jobs}, "
                    f"not fully read {collection.failed_boards}, new postings {collection.postings_new}, "
                    f"closed {collection.closed}, reopened {collection.reopened}, unknown {collection.unknown}, "
                    f"closures held {collection.held} on {collection.braked_boards}, "
                    f"cleaning errors {collection.clean_errors}",
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
