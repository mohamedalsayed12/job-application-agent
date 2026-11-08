"""Add companies from a hand-written list: ``python -m app.discovery.seed``.

A seed row is only a lead. Its domain and any board id still go through the
board lookup and the verifier, and nothing here marks a company as checked.
Each row keeps a seed evidence quote so audits can tell seeded companies from
discovered ones.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.discovery.run import DiscoveryReport, lookup_and_verify
from app.fetch.gateway import FetchGateway
from app.models import Candidate, CandidateEvidence, ExtractedBy
from app.queue import ingest_candidate
from app.queue.agencies import is_recruitment_agency

DEFAULT_SEED = Path(__file__).parent / "data" / "uk_tech_seed.csv"
SEED_QUOTE = "seed list: not discovered by search"
_BOARD_INSTANCE = {"greenhouse": "greenhouse", "lever": "lever_global"}


@dataclass(frozen=True, slots=True)
class SeedRow:
    company_name: str
    domain: str
    board_system: str | None = None
    board_id: str | None = None


def load_seed(path: Path = DEFAULT_SEED) -> list[SeedRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            SeedRow(
                company_name=row["company_name"].strip(),
                domain=row["domain"].strip().lower(),
                board_system=row["board_system"].strip() or None,
                board_id=row["board_id"].strip() or None,
            )
            for row in csv.DictReader(handle)
        ]


def ingest_seed(session: Session, rows: list[SeedRow]) -> list[Candidate]:
    """Queue seed rows through normal dedupe, skipping agencies."""
    touched: list[Candidate] = []
    for row in rows:
        if is_recruitment_agency(row.company_name):
            continue
        result = ingest_candidate(
            session,
            company_name=row.company_name,
            extracted_by=ExtractedBy.code,
            domain_candidates=[row.domain],
            board_system=row.board_system,
            board_instance=_BOARD_INSTANCE.get(row.board_system or ""),
            board_id=row.board_id,
            evidence=[(None, SEED_QUOTE, f"https://{row.domain}")],
        )
        touched.append(result.survivor)
    session.flush()
    return touched


def seeded_candidate_ids(session: Session) -> set[uuid.UUID]:
    return set(
        session.scalars(
            select(CandidateEvidence.candidate_id).where(CandidateEvidence.quoted_text == SEED_QUOTE)
        )
    )


async def _main(path: Path, max_verify: int) -> None:
    session = SessionLocal()
    gateway = FetchGateway()
    try:
        touched = ingest_seed(session, load_seed(path))
        session.commit()
        print(f"seeded {len(touched)} companies")
        report = DiscoveryReport()
        await lookup_and_verify(
            session, gateway, report=report, max_verify=max_verify, commit=session.commit, log=print
        )
    finally:
        await gateway.aclose()
        session.close()
    for line in report.lines():
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.discovery.seed", description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--max-verify", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(_main(args.file, args.max_verify))


if __name__ == "__main__":
    main()
