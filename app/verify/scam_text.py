"""Indeed-style text scam heuristics for job adverts (D55).

One list used by check_real, verify_jobs, and the job page gate.
"""

from __future__ import annotations

import re

# Payment and fintech adverts talk about fees and payments all the time, so fee
# patterns need the request itself. Indeed UK: payment, chat-only, get-rich.
_SCAM_SIGNS = (
    (
        "asks the applicant for money",
        re.compile(
            r"\b(?:registration|training|processing|admin(?:istration)?) fees?\b"
            r"|\bpay (?:a|an|the|your) (?:\w+ )?(?:fee|deposit)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "chat-only contact",
        re.compile(
            r"\b(?:contact|message|text|reach|apply)\b[^.]{0,40}\b(?:whatsapp|telegram)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "promises quick wealth",
        re.compile(
            r"\b(?:get rich(?: quick)?|become (?:a )?millionaire|earn (?:unlimited|huge) (?:money|income|wealth)"
            r"|wealth in (?:days|weeks))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "vague requirements only",
        re.compile(
            r"(?:must be (?:of )?legal age|over 18|18\+|know how to type|able to (?:read|type))"
            r".{0,120}?(?:that'?s (?:all|it)|no (?:experience|skills?) (?:needed|required))",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def scam_reasons(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(reason for reason, pattern in _SCAM_SIGNS if pattern.search(text))
