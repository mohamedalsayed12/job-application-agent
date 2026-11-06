"""Mark leftover pages that contain common prompt-injection phrases (MVP §10.2 / T12)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

from app.extraction.ai_types import LeftoverResult

# Case-insensitive phrases that try to steer the model. Expand carefully; false
# positives only mark suspicious — they never auto-verify.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?(your\s+)?(previous\s+)?(rules|instructions)",
        r"disregard\s+(your\s+)?(system\s+)?(prompt|instructions|rules)",
        r"mark\s+(this|me|us)\s+(as\s+)?verified",
        r"you\s+are\s+now\s+(in\s+)?(admin|developer|jailbreak)",
        r"system\s*:\s*override",
        r"<\s*/?\s*system\s*>",
        r"do\s+not\s+follow\s+your\s+(safety\s+)?rules",
    )
)


def scan_suspicious(results: Iterable[LeftoverResult]) -> frozenset[uuid.UUID]:
    """Return leftover result ids whose text matches injection patterns."""
    suspicious: set[uuid.UUID] = set()
    for row in results:
        blob = f"{row.title}\n{row.snippet}\n{row.url}"
        if any(pattern.search(blob) for pattern in _INJECTION_PATTERNS):
            suspicious.add(row.id)
    return frozenset(suspicious)
