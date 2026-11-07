"""Offline fixture template CSV (no DB writes). Canonical JSON lives in-package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.audit.csv_export import write_audit_csv
from app.audit.sample import round_robin_take
from app.audit.types import CLASSIFIED_STATES, AuditSheetRow

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "data" / "classified_companies.json"


def load_fixture_companies(path: Path | None = None) -> list[dict[str, Any]]:
    data = json.loads((path or DEFAULT_FIXTURE).read_text(encoding="utf-8"))
    companies = data["companies"]
    if not isinstance(companies, list):
        raise ValueError("fixture companies must be a list")
    return companies


def write_fixture_template_csv(
    out: Path,
    *,
    path: Path | None = None,
    size: int = 20,
) -> tuple[Path, dict[str, int]]:
    """Write an operator template CSV from fixture JSON without touching the DB.

    Ids are ephemeral. Replace after a real ``create_audit_sheet`` run.
    """
    companies = load_fixture_companies(path)
    buckets: dict[str, list[dict[str, Any]]] = {state: [] for state in CLASSIFIED_STATES}
    for raw in companies:
        state = str(raw["state"])
        if state not in buckets:
            raise ValueError(f"fixture state not classified: {state}")
        buckets[state].append(raw)

    picked = round_robin_take(buckets, size=size)
    if len(picked) < size:
        raise ValueError(f"fixture has {len(picked)} rows, need {size}")

    rows: list[AuditSheetRow] = []
    counts: dict[str, int] = {str(state): 0 for state in CLASSIFIED_STATES}
    for raw in picked:
        assert isinstance(raw, dict)
        state = str(raw["state"])
        counts[state] = counts.get(state, 0) + 1
        urls = tuple(
            str(u)
            for u in (raw.get("evidence_urls") or []) + (raw.get("check_urls") or [])
            if str(u).lower().startswith(("http://", "https://"))
        )
        rows.append(
            AuditSheetRow(
                audit_sample_id=uuid4(),
                candidate_id=uuid4(),
                company_name=str(raw["company_name"]),
                cleaned_name=str(raw["cleaned_name"]),
                primary_domain=raw.get("primary_domain"),
                board_system=raw.get("board_system"),
                board_instance=raw.get("board_instance"),
                board_id=raw.get("board_id"),
                system_result=state,
                evidence_urls=urls,
            )
        )
    return write_audit_csv(out, rows), counts
