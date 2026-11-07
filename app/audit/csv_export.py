"""Write the operator-fillable audit CSV. Never invents auditor judgments."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

from app.audit.types import CSV_COLUMNS, AuditSheetRow

_EVIDENCE_SEP = " | "
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str) -> str:
    """Neutralize Excel formula injection on untrusted discovery text."""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def sheet_row_to_csv_dict(row: AuditSheetRow) -> dict[str, str]:
    """Map a sheet row to CSV cells. Operator columns are always blank."""
    return {
        "audit_sample_id": str(row.audit_sample_id),
        "candidate_id": str(row.candidate_id),
        "company_name": _safe_cell(row.company_name),
        "cleaned_name": _safe_cell(row.cleaned_name),
        "primary_domain": _safe_cell(row.primary_domain or ""),
        "board_system": _safe_cell(row.board_system or ""),
        "board_instance": _safe_cell(row.board_instance or ""),
        "board_id": _safe_cell(row.board_id or ""),
        "system_result": row.system_result,
        "evidence_urls": _safe_cell(_EVIDENCE_SEP.join(row.evidence_urls)),
        "auditor_result": "",
        "is_correct": "",
        "is_useful": "",
        "notes": "",
    }


def csv_text(rows: Sequence[AuditSheetRow]) -> str:
    """In-memory CSV (UTF-8)."""
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(sheet_row_to_csv_dict(row))
    return buf.getvalue()


def write_audit_csv(path: Path, rows: Sequence[AuditSheetRow]) -> Path:
    """Write UTF-8-SIG CSV so Excel on Windows keeps non-ASCII names."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(csv_text(rows))
    return path
