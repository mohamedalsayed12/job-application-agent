"""Spot common prompt-injection phrases in outside text before it reaches the AI (T12)."""

from __future__ import annotations

import re

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


def looks_like_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
