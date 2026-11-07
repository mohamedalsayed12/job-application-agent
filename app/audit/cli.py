"""Operator CLI: sample classified companies and write the audit CSV.

Examples::

    uv run python -m app.audit --commit --out step10-audit-sample.csv
    uv run python -m app.audit --from-fixtures --out step10-audit-sample.csv

Does not judge classifications. ``--from-fixtures`` writes a template CSV with
ephemeral ids and never inserts candidates. Live sampling requires ``--commit``
so CSV ids match persisted ``audit_sample`` rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.audit.csv_export import write_audit_csv
from app.audit.fixtures import write_fixture_template_csv
from app.audit.sample import InsufficientClassifiedError, create_audit_sheet
from app.audit.types import DEFAULT_SAMPLE_SIZE, MAX_SAMPLE_SIZE
from app.core.config import get_settings
from app.core.db import SessionLocal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create audit_sample rows and export an operator-fillable CSV."
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path for the CSV (operator fills auditor_result / is_correct / notes).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Sample size (default {DEFAULT_SAMPLE_SIZE}, max {MAX_SAMPLE_SIZE}).",
    )
    parser.add_argument(
        "--from-fixtures",
        action="store_true",
        help="Write offline template CSV from fixture JSON (no DB writes).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Required for live DB sampling so CSV ids match persisted rows.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.size < 1 or args.size > MAX_SAMPLE_SIZE:
        print(f"size must be 1–{MAX_SAMPLE_SIZE}", file=sys.stderr)
        return 2

    if args.from_fixtures:
        path, counts = write_fixture_template_csv(args.out, size=args.size)
        print(
            f"wrote fixture template {path} size={args.size} by_state={counts} "
            "(ephemeral ids; not live discovery — re-run with --commit after real classification)",
            file=sys.stderr,
        )
        return 0

    if not args.commit:
        print(
            "live sampling requires --commit so CSV audit_sample_id values match the DB",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    missing = [
        name
        for name, present in {
            "reed_api_key": bool(settings.reed_api_key.strip()),
            "serpapi_api_key": bool(settings.serpapi_api_key.strip()),
        }.items()
        if not present
    ]
    if missing:
        print(
            "provider keys missing (presence only): "
            + ", ".join(missing)
            + " — £0 path needs REED_API_KEY + SERPAPI_API_KEY for live discovery; "
            "money_cap can stay unset. Use --from-fixtures for an offline template.",
            file=sys.stderr,
        )

    session = SessionLocal()
    try:
        rows, summary = create_audit_sheet(session, size=args.size)
        session.commit()
        path = write_audit_csv(args.out, rows)
    except InsufficientClassifiedError as exc:
        session.rollback()
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(
        f"wrote {path} selected={summary.selected} "
        f"by_state={summary.by_state} available={summary.classified_available}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
