"""Write code extractions as ``candidate`` + ``candidate_evidence`` rows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.extraction.types import CodeHit
from app.models import Candidate, ExtractedBy
from app.queue.agencies import is_recruitment_agency
from app.queue.dedupe import ingest_candidate


def persist_code_hits(session: Session, hits: list[CodeHit]) -> list[Candidate]:
    """Insert or merge each hit through queue dedupe (MVP §9)."""
    touched: list[Candidate] = []
    seen: set[object] = set()
    for hit in hits:
        if hit.company_name and is_recruitment_agency(hit.company_name):
            continue
        result = ingest_candidate(
            session,
            company_name=hit.company_name or (hit.board.board_id if hit.board else ""),
            extracted_by=ExtractedBy.code,
            board_system=hit.board.system if hit.board else None,
            board_instance=hit.board.instance if hit.board else None,
            board_id=hit.board.board_id if hit.board else None,
            evidence=[(hit.search_result_id, hit.quoted_text, hit.source_url)],
        )
        key = result.survivor.id
        if key not in seen:
            seen.add(key)
            touched.append(result.survivor)
    return touched
