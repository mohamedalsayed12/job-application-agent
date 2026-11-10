"""Summarise every matched job that has no summary yet: ``python -m app.summaries``."""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.fetch.gateway import FetchGateway
from app.summaries.generate import summarise_matches


async def _main() -> None:
    gateway = FetchGateway()
    try:
        with SessionLocal() as session:
            report = await summarise_matches(session, gateway=gateway, settings=get_settings(), persist=session.commit)
    finally:
        await gateway.aclose()
    print(f"summaries {dict(report.outcomes)}, stopped by {report.stopped_by or 'nothing'}")


if __name__ == "__main__":
    asyncio.run(_main())
