"""Step 16: list the open jobs that match on each audited company's verified board.

The operator judges usefulness, so this never fills it in. It only reads the database.

    uv run python -m app.audit.usefulness --sheet docs/step10-audit-sample-4.csv \\
        --out-sheet docs/step16-audit-usefulness.csv --out-jobs docs/step16-audit-jobs.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.csv_export import _safe_cell
from app.core.db import SessionLocal
from app.models import (
    ApplicantJobMatch,
    ApplicantPreference,
    BoardStatus,
    EmployerBoard,
    Job,
    JobFreshness,
    JobLink,
    JobPosting,
)

NOT_VERIFIED = "n/a (not verified)"
SHEET_COLUMNS = (
    "audit_sample_id",
    "company_name",
    "primary_domain",
    "board_system",
    "board_id",
    "system_result",
    "board_verified_now",
    "matching_open_jobs",
    "matching_open_jobs_by_profile",
    "usefulness",
    "usefulness_notes",
)
JOB_COLUMNS = (
    "company_name",
    "applicant_profile",
    "job_title",
    "places",
    "remote_type",
    "role_fit",
    "level",
    "location_fit",
    "salary_status",
    "last_confirmed_open",
    "apply_url",
)

BoardKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class JobMatch:
    board: BoardKey
    job_id: Any
    cells: dict[str, str]


def _board_key(row: dict[str, str]) -> BoardKey | None:
    key = (row["board_system"], row["board_instance"], row["board_id"])
    return key if all(key) else None


def _profile(preference: ApplicantPreference) -> str:
    return f"{', '.join(preference.target_roles)} in {', '.join(preference.locations)}"


def _place_names(places: list[dict[str, Any]]) -> str:
    return "; ".join(p["city"] or p["country"] or p["region"] or "" for p in places)


def _verified_boards(session: Session) -> set[BoardKey]:
    rows = session.execute(
        select(EmployerBoard.system, EmployerBoard.instance, EmployerBoard.board_id).where(
            EmployerBoard.status == BoardStatus.verified
        )
    )
    return {tuple(row) for row in rows}


def _job_matches(session: Session, boards: set[BoardKey]) -> list[JobMatch]:
    profiles = {p.applicant_id: _profile(p) for p in session.scalars(select(ApplicantPreference))}
    rows = session.execute(
        select(ApplicantJobMatch, Job, EmployerBoard)
        .join(Job, Job.id == ApplicantJobMatch.job_id)
        .join(JobLink, JobLink.job_id == Job.id)
        .join(JobPosting, JobPosting.id == JobLink.job_posting_id)
        .join(EmployerBoard, EmployerBoard.id == JobPosting.employer_board_id)
        .where(
            ApplicantJobMatch.location_fit.is_(True),
            Job.freshness == JobFreshness.open,
        )
    ).all()
    found: dict[tuple[BoardKey, Any, Any], JobMatch] = {}
    for match, job, board in rows:
        key = (board.system, board.instance, board.board_id)
        if key not in boards:
            continue
        role = match.reasons["role"]
        found[(key, match.applicant_id, job.id)] = JobMatch(
            key,
            job.id,
            {
                "applicant_profile": profiles[match.applicant_id],
                "job_title": job.title,
                "places": _place_names(job.places),
                "remote_type": job.remote_type or "",
                "role_fit": role["kind"],
                "level": role["level"] or "",
                "location_fit": match.reasons["location"],
                "salary_status": match.salary_status,
                "last_confirmed_open": job.last_seen_at.date().isoformat(),
                "apply_url": job.apply_url,
            },
        )
    return sorted(
        found.values(),
        key=lambda m: (m.board, m.cells["applicant_profile"], m.cells["role_fit"], m.cells["job_title"]),
    )


def build_rows(session: Session, sheet: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """One row per audited company, and one row per job and applicant profile that fit."""
    verified = _verified_boards(session)
    sampled = {key for row in sheet if (key := _board_key(row)) and key in verified}
    matches = _job_matches(session, sampled)
    company = {key: row["company_name"] for row in sheet if (key := _board_key(row))}

    sheet_rows = []
    for row in sheet:
        key = _board_key(row)
        on_board = [m for m in matches if m.board == key]
        by_profile = Counter(m.cells["applicant_profile"] for m in on_board)
        is_verified = key in sampled
        sheet_rows.append(
            {
                "audit_sample_id": row["audit_sample_id"],
                "company_name": _safe_cell(row["company_name"]),
                "primary_domain": _safe_cell(row["primary_domain"]),
                "board_system": row["board_system"],
                "board_id": _safe_cell(row["board_id"]),
                "system_result": row["system_result"],
                "board_verified_now": "yes" if is_verified else "no",
                "matching_open_jobs": str(len({m.job_id for m in on_board})) if is_verified else "",
                "matching_open_jobs_by_profile": " | ".join(f"{p}: {n}" for p, n in sorted(by_profile.items())),
                "usefulness": "" if is_verified else NOT_VERIFIED,
                "usefulness_notes": "",
            }
        )
    job_rows = [
        {column: _safe_cell(value) for column, value in {"company_name": company[m.board], **m.cells}.items()}
        for m in matches
    ]
    return sheet_rows, job_rows


def read_sheet(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.audit.usefulness", description=__doc__)
    parser.add_argument("--sheet", type=Path, required=True, help="the audit sheet that lists the companies")
    parser.add_argument("--out-sheet", type=Path, required=True, help="one row per company, with a blank usefulness column")
    parser.add_argument("--out-jobs", type=Path, required=True, help="the matching open jobs, with links")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        sheet_rows, job_rows = build_rows(session, read_sheet(args.sheet))
    _write(args.out_sheet, SHEET_COLUMNS, sheet_rows)
    _write(args.out_jobs, JOB_COLUMNS, job_rows)
    to_judge = sum(1 for row in sheet_rows if not row["usefulness"])
    print(f"{len(sheet_rows)} companies, {to_judge} verified boards to judge, {len(job_rows)} job rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
