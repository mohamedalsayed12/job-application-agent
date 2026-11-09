"""Certifications and licences an advert requires, and whether an applicant holds them (D40).

Rules only. Each group is one requirement that any of its credentials meets, because adverts
list them as alternatives: "Qualified accountant (ACA, ACCA, CIMA or equivalent)".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Credential:
    name: str
    in_advert: re.Pattern[str]
    on_profile: re.Pattern[str]


def _credential(name: str, acronyms: str = "", phrases: str = "") -> Credential:
    """Acronyms must keep their capitals in an advert. People type them however they like."""
    parts = [rf"\b(?:{acronyms})(?![\w+])" if acronyms else "", rf"(?i:{phrases})" if phrases else ""]
    pattern = "|".join(part for part in parts if part)
    return Credential(name, re.compile(pattern), re.compile(pattern.replace("(?i:", "(?:"), re.IGNORECASE))


# The groups found in 1,837 real adverts on 2026-11-09, plus clearance and cloud certificates.
CREDENTIALS = (
    _credential("UK driving licence", phrases=r"\bdriv(?:ing|er'?s?) licen[cs]e\b"),
    _credential("Gas Safe registration", phrases=r"\bgas safe\b"),
    _credential("accountancy qualification", acronyms="ACA|ACCA|CIMA|ACMA|CPA", phrases=r"\bchartered accountant\b"),
    _credential("project management certification", acronyms="PMP|PgMP|PRINCE2"),
    _credential(
        "security certification",
        acronyms="CISSP|CISM|CISA|GIAC|CEH|OSCP",
        phrases=r"\b(?:comptia )?security\+",
    ),
    _credential("cloud certification", phrases=r"\b(?:aws|azure|google cloud|gcp) certified\b"),
    _credential(
        "security clearance",
        acronyms="BPSS|NPPV",
        phrases=r"\b(?:sc|dv|security|edv) clearance\b|\bdeveloped vetting\b",
    ),
)

# Wording close to a credential that makes it optional, or something to get later.
_OPTIONAL = re.compile(
    r"\b(?:prefer\w*|desirable|nice to have|bonus|a plus|advantage\w*|ideally|working towards|"
    r"close to qualification|eligible|eligibility|able to obtain|willing to obtain)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?;\n]")
# Cleaned adverts lose their bullet breaks, so the wording must be close: "preferably PMP",
# "(CPA strongly preferred)". Further away it usually belongs to the next bullet.
_BEFORE = 70
_AFTER = 40
# A bracketed aside that doesn't hold the credential is about something else: "(Netsuite preferred)".
_ASIDE = re.compile(r"\([^()]*\)")


def required_in_advert(description: str | None) -> list[str]:
    """Groups the advert asks for, in table order. A group named only as optional is left out."""
    if not description:
        return []
    return [credential.name for credential in CREDENTIALS if _required(credential, description)]


def _required(credential: Credential, text: str) -> bool:
    for match in credential.in_advert.finditer(text):
        start = max(_sentence_start(text, match.start()), match.start() - _BEFORE)
        end = min(_sentence_end(text, match.end()), match.end() + _AFTER)
        before = _ASIDE.sub(" ", text[start : match.start()])
        # A ")" left over closes an aside that began before the window.
        before = before[before.rfind(")") + 1 :]
        if not _OPTIONAL.search(before) and not _OPTIONAL.search(_after_without_asides(text[match.end() : end])):
            return True
    return False


def _after_without_asides(after: str) -> str:
    """Keeps the rest of an aside the credential sits in, "ACA preferred)", and drops later ones."""
    close, opening = after.find(")"), after.find("(")
    if close >= 0 and (opening < 0 or close < opening):
        return after[: close + 1] + _ASIDE.sub(" ", after[close + 1 :])
    return _ASIDE.sub(" ", after)


def _sentence_start(text: str, position: int) -> int:
    ends = [found.end() for found in _SENTENCE_END.finditer(text, 0, position)]
    return ends[-1] if ends else 0


def _sentence_end(text: str, position: int) -> int:
    found = _SENTENCE_END.search(text, position)
    return found.start() if found else len(text)


@dataclass(frozen=True, slots=True)
class Held:
    name: str
    issuer: str | None
    expires_on: date | None


def held_groups(held: Iterable[Held], *, today: date) -> set[str]:
    """Groups the applicant holds. An expired certificate doesn't count."""
    groups = set()
    for item in held:
        if item.expires_on is not None and item.expires_on < today:
            continue
        typed = f"{item.name} {item.issuer or ''}"
        groups |= {credential.name for credential in CREDENTIALS if credential.on_profile.search(typed)}
        groups |= {credential.name for credential in CREDENTIALS if credential.name.casefold() == item.name.casefold()}
    return groups
