"""Operator CLI: sample classified companies and write the audit CSV.

Examples::

    uv run python -m app.audit.cli --out /tmp/step10-audit-sample.csv
    uv run python -m app.audit.cli --seed-fixtures --out docs/step10-audit-sample.csv

Does not judge classifications. Live discovery is out of scope for this module;
seed fixtures when Reed/SerpAPI keys are missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.audit.csv_export import write_audit_csv
from app.audit.fixtures import seed_classified_from_fixture
from app.audit.sample import InsufficientClassifiedError, create_audit_sheet
from app.audit.types import DEFAULT_SAMPLE_SIZE
from app.core.config import get_settings
from app.core.db import SessionLocal


def _provider_status() -> dict[str, bool]:
    settings = get_settings()
    return {
        "reed_api_key": bool(settings.reed_api_key.strip()),
        "serpapi_api_key": bool(settings.serpapi_api_key.strip()),
        "openai_api_key": bool(settings.openai_api_key.strip()),
    }


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
        help=f"Sample size (default {DEFAULT_SAMPLE_SIZE}).",
    )
    parser.add_argument(
        "--seed-fixtures",
        action="store_true",
        help="Insert offline classified fixtures before sampling (no live keys).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit DB changes (default is dry-run / rollback).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = _provider_status()
    missing = [name for name, present in status.items() if not present]
    if missing:
        print(
            "provider keys missing (presence only): " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "£0 path: set REED_API_KEY + SERPAPI_API_KEY for live discovery; "
            "money_cap can stay unset. Use --seed-fixtures for an offline sheet.",
            file=sys.stderr,
        )

    session = SessionLocal()
    try:
        if args.seed_fixtures:
            seeded = seed_classified_from_fixture(session)
            print(f"seeded {len(seeded)} classified fixture companies", file=sys.stderr)
        rows, summary = create_audit_sheet(session, size=args.size)
        path = write_audit_csv(args.out, rows)
        if args.commit:
            session.commit()
        else:
            session.rollback()
            print("rolled back DB writes (pass --commit to persist)", file=sys.stderr)
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
