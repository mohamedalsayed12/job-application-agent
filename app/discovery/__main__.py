"""Run one discovery pass for every applicant: ``uv run python -m app.discovery``."""

from __future__ import annotations

import argparse
import asyncio

from app.core.db import SessionLocal
from app.discovery.run import run_discovery
from app.fetch.gateway import FetchGateway


async def _main(args: argparse.Namespace) -> None:
    session = SessionLocal()
    gateway = FetchGateway()
    try:
        report = await run_discovery(
            session,
            gateway,
            slots_per_applicant=args.slots,
            max_cf3=args.max_cf3,
            max_verify=args.max_verify,
            persist=session.commit,
            log=print,
        )
    finally:
        await gateway.aclose()
        session.close()
    for line in report.lines():
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.discovery", description=__doc__)
    parser.add_argument("--slots", type=int, default=5, help="search slots per applicant")
    parser.add_argument("--max-cf3", type=int, default=60, help="CF3 searches this pass")
    parser.add_argument("--max-verify", type=int, default=200, help="verifications this pass")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
