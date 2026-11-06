"""Cache leftover AI calls by input hash + model + prompt version (MVP §10.6)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.ai_types import LeftoverResult
from app.models import AiExtraction


def input_hash(
    results: Sequence[LeftoverResult],
    *,
    model: str,
    prompt_version: str,
) -> str:
    rows = [
        {
            "id": str(row.id),
            "url": row.url,
            "title": row.title,
            "snippet": row.snippet,
        }
        for row in sorted(results, key=lambda item: str(item.id))
    ]
    payload = {
        "model": model,
        "prompt_version": prompt_version,
        "results": rows,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lookup_cached(
    session: Session,
    *,
    digest: str,
    prompt_version: str,
) -> AiExtraction | None:
    return session.scalars(
        select(AiExtraction).where(
            AiExtraction.input_hash == digest,
            AiExtraction.prompt_version == prompt_version,
        )
    ).first()
