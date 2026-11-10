"""Step 16: the audit sheet's matching jobs, with usefulness left for the operator."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.audit.usefulness import (
    JOB_COLUMNS,
    NOT_VERIFIED,
    SHEET_COLUMNS,
    build_rows,
    main,
    read_sheet,
)
from app.matching.match import match_all
from app.models import BoardStatus, EmployerBoard, JobFreshness
from tests.test_cleaning import _board, _Collector, _job
from tests.test_matching import SOON, _applicant, _gh


def _sheet_row(company: str = "Acme", *, board_id: str = "acme", result: str = "verified") -> dict[str, str]:
    return {
        "audit_sample_id": f"id-{company}",
        "company_name": company,
        "primary_domain": f"{company.lower()}.example",
        "board_system": "greenhouse" if board_id else "",
        "board_instance": "greenhouse" if board_id else "",
        "board_id": board_id,
        "system_result": result,
        "is_useful": "yes",
        "notes": "Useful: an earlier opinion",
    }


async def _collected(session: Session, jobs: list[dict]) -> EmployerBoard:
    board = _board(session)
    await _Collector(session).read(200, {"jobs": jobs})
    return board


async def test_a_verified_board_lists_its_open_matching_jobs_with_links(session: Session) -> None:
    await _collected(session, [_gh(1), _gh(2, city="Berlin, Germany"), _gh(3, title="Account Manager")])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)

    sheet, jobs = build_rows(session, [_sheet_row()])

    assert [job["apply_url"] for job in jobs] == ["https://job-boards.greenhouse.io/acme/jobs/1"]
    assert jobs[0]["company_name"] == "Acme"
    assert jobs[0]["applicant_profile"] == "Backend Developer in Manchester"
    assert jobs[0]["role_fit"] == "exact"
    assert jobs[0]["places"] == "Manchester"
    assert jobs[0]["last_confirmed_open"]
    assert sheet[0]["board_verified_now"] == "yes"
    assert sheet[0]["matching_open_jobs"] == "1"
    assert sheet[0]["matching_open_jobs_by_profile"] == "Backend Developer in Manchester: 1"


async def test_usefulness_is_left_blank_for_every_verified_board(session: Session) -> None:
    await _collected(session, [_gh(1)])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)

    sheet, _ = build_rows(session, [_sheet_row()])

    assert sheet[0]["usefulness"] == ""
    assert sheet[0]["usefulness_notes"] == ""


async def test_a_board_with_no_matches_is_still_for_the_operator_to_judge(session: Session) -> None:
    await _collected(session, [_gh(1, title="Account Manager")])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)

    sheet, jobs = build_rows(session, [_sheet_row()])

    assert jobs == []
    assert sheet[0]["matching_open_jobs"] == "0"
    assert sheet[0]["usefulness"] == ""


def test_companies_without_a_verified_board_are_marked_not_applicable(session: Session) -> None:
    sheet, jobs = build_rows(
        session,
        [_sheet_row("Barclays", board_id="", result="needs_review"), _sheet_row("Tatcha", board_id="tatcha", result="needs_review")],
    )

    assert [row["usefulness"] for row in sheet] == [NOT_VERIFIED, NOT_VERIFIED]
    assert [row["board_verified_now"] for row in sheet] == ["no", "no"]
    assert jobs == []


async def test_a_board_no_longer_verified_is_not_applicable(session: Session) -> None:
    board = await _collected(session, [_gh(1)])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)
    board.status = BoardStatus.retired
    session.flush()

    sheet, jobs = build_rows(session, [_sheet_row()])

    assert sheet[0]["usefulness"] == NOT_VERIFIED
    assert jobs == []


async def test_closed_jobs_and_boards_outside_the_sheet_are_left_out(session: Session) -> None:
    await _collected(session, [_gh(1), _gh(2, title="Backend Engineer")])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)
    _job(session, 2).freshness = JobFreshness.closed
    session.flush()

    _, jobs = build_rows(session, [_sheet_row()])
    assert [job["job_title"] for job in jobs] == ["Backend Developer"]

    _, none = build_rows(session, [_sheet_row("Other", board_id="other", result="verified")])
    assert none == []


async def test_one_job_fitting_two_profiles_gets_two_rows_but_counts_once(session: Session) -> None:
    await _collected(session, [_gh(1)])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    _applicant(session, roles=["Software Engineer"], locations=["Manchester"])
    match_all(session, now=SOON)

    sheet, jobs = build_rows(session, [_sheet_row()])

    assert len(jobs) == 2
    assert sheet[0]["matching_open_jobs"] == "1"


async def test_a_job_title_cannot_become_a_spreadsheet_formula(session: Session) -> None:
    await _collected(session, [_gh(1, title="=HYPERLINK(1) Backend Developer")])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)

    _, jobs = build_rows(session, [_sheet_row()])

    assert jobs[0]["job_title"].startswith("'=")


async def test_no_applicant_email_and_no_earlier_opinion_reach_the_files(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _collected(session, [_gh(1)])
    _applicant(session, roles=["Backend Developer"], locations=["Manchester"])
    match_all(session, now=SOON)
    source = tmp_path / "sheet.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_sheet_row()))
        writer.writeheader()
        writer.writerow(_sheet_row())

    class _Same:
        def __enter__(self) -> Session:
            return session

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.audit.usefulness.SessionLocal", _Same)
    out_sheet, out_jobs = tmp_path / "out.csv", tmp_path / "jobs.csv"
    assert main(["--sheet", str(source), "--out-sheet", str(out_sheet), "--out-jobs", str(out_jobs)]) == 0

    written = read_sheet(out_sheet)
    assert tuple(written[0]) == SHEET_COLUMNS
    assert tuple(read_sheet(out_jobs)[0]) == JOB_COLUMNS
    text = out_sheet.read_text(encoding="utf-8-sig") + out_jobs.read_text(encoding="utf-8-sig")
    assert "@example.com" not in text
    assert "earlier opinion" not in text
