"""Write the operator-fillable audit CSV. Never invents auditor judgments."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from app.audit.types import CSV_COLUMNS, AuditSheetRow

_EVIDENCE_SEP = " | "


def sheet_row_to_csv_dict(row: AuditSheetRow) -> dict[str, str]:
    """Map a sheet row to CSV cells. Operator columns are always blank."""
    return {
        "audit_sample_id": str(row.audit_sample_id),
        "candidate_id": str(row.candidate_id),
        "company_name": row.company_name,
        "cleaned_name": row.cleaned_name,
        "primary_domain": row.primary_domain or "",
        "board_system": row.board_system or "",
        "board_instance": row.board_instance or "",
        "board_id": row.board_id or "",
        "system_result": row.system_result,
        "evidence_urls": _EVIDENCE_SEP.join(row.evidence_urls),
        "auditor_result": "",
        "is_correct": "",
        "is_useful": "",
        "notes": "",
    }


def write_audit_csv(path: Path, rows: Sequence[AuditSheetRow]) -> Path:
    """Write UTF-8 CSV with header. Creates parent dirs if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow(sheet_row_to_csv_dict(row))
    return path


def csv_text(rows: Sequence[AuditSheetRow]) -> str:
    """In-memory CSV for admin download responses."""
    from io import StringIO

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(sheet_row_to_csv_dict(row))
    return buf.getvalue()
