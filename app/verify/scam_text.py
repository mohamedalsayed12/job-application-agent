"""Indeed-style text scam heuristics for job adverts (D55).

One list used by check_real, verify_jobs, and the job page gate.

Each sign is checked one sentence at a time, so reassurance in the same advert does not trip it:
"We will never ask you to pay a fee" or "contact us on WhatsApp or by email" are what honest
adverts say. A hit is only a strong hint, not a verdict: verify_jobs still lets the company's own
careers page clear a job (D55).
"""

from __future__ import annotations

import re

# Bump when the signs below change, so jobs flagged by an older version are checked again.
SCAM_RULES_VERSION = 2

_SENTENCE = re.compile(r"(?<=[.!?\n])\s+|\n+")

# Payment and fintech adverts talk about fees and payments all the time, so fee
# patterns need the request itself.
_FEE = re.compile(
    r"\b(?:registration|training|processing|admin(?:istration)?|starter|joining|onboarding|application) fees?\b"
    r"|\bpay (?:a|an|the|your|for) (?:\w+ )?(?:fee|deposit|starter kit|training|uniform)\b"
    r"|\b(?:deposit|fee) (?:of|is) [£$€]\s?\d",
    re.IGNORECASE,
)
# A sentence saying no fee is asked, or that the employer pays it.
_FEE_DENIED = re.compile(
    r"\b(?:no|never|not|don'?t|doesn'?t|won'?t|will not|without|free of|at no cost|zero|waived)\b"
    r"|\b(?:paid|covered|funded|met) (?:for )?by (?:us|the company|the employer|[A-Z]\w+)\b"
    r"|\bwe (?:pay|cover|fund)\b",
    re.IGNORECASE,
)

_CHAT_APP = r"(?:whatsapp|telegram|signal app|wechat)"
# Applying, or sending a CV, through a chat app is the scam sign, as is "only" that way.
_CHAT_APPLY = re.compile(
    rf"\b(?:apply|send(?:ing)? (?:us )?(?:your )?(?:cv|resume|application|details))\s+(?:[\w']+\s+){{0,5}}?"
    rf"(?:via|on|through|by|using|over)\s+{_CHAT_APP}\b"
    rf"|\b{_CHAT_APP}\b[^.,;]{{0,30}}\bto (?:apply|send (?:us )?(?:your )?(?:cv|resume|application))\b",
    re.IGNORECASE,
)
_CHAT_ONLY = re.compile(
    rf"\b(?:only|exclusively|strictly|solely)\b[^.]{{0,40}}\b{_CHAT_APP}\b|\b{_CHAT_APP}\b[^.]{{0,25}}\bonly\b",
    re.IGNORECASE,
)
_OTHER_CONTACT = re.compile(r"\b(?:e-?mail|website|online|careers? (?:page|site)|apply (?:here|now|below))\b|@\w", re.IGNORECASE)

_QUICK_WEALTH = re.compile(
    r"\b(?:get rich(?: quick)?|become (?:a )?millionaire|earn (?:unlimited|huge) (?:money|income|wealth)"
    r"|wealth in (?:days|weeks))\b",
    re.IGNORECASE,
)

# "Over 18, know how to type, that's all" is a scam sign. "Must be over 18, no experience
# required" on its own is an ordinary entry-level advert.
_VAGUE = re.compile(
    r"(?:must be (?:of )?legal age|over 18|18\+|know how to type|able to (?:read|type)|have a (?:smart)?phone)"
    r".{0,120}?(?:that'?s (?:all|it)|all you need|nothing else (?:is )?(?:needed|required))",
    re.IGNORECASE | re.DOTALL,
)


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE.split(text) if part.strip()]


def _asks_for_money(sentences: list[str]) -> bool:
    return any(_FEE.search(s) and not _FEE_DENIED.search(s) for s in sentences)


def _chat_only(sentences: list[str]) -> bool:
    for sentence in sentences:
        if _OTHER_CONTACT.search(sentence):
            continue
        if _CHAT_APPLY.search(sentence) or _CHAT_ONLY.search(sentence):
            return True
    return False


def scam_reasons(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    sentences = _sentences(text)
    reasons = []
    if _asks_for_money(sentences):
        reasons.append("asks the applicant for money")
    if _chat_only(sentences):
        reasons.append("chat-only contact")
    if _QUICK_WEALTH.search(text):
        reasons.append("promises quick wealth")
    if _VAGUE.search(text):
        reasons.append("vague requirements only")
    return tuple(reasons)
