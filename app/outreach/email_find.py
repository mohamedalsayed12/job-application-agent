"""Find a contact's work email (D76).

Without a provider key the address is guessed from the employer's domain in the most common company format
(first.last@) and marked as a guess, with the other usual formats beside it. With HUNTER_API_KEY set, Hunter's
email finder looks the person up and says how sure it is; that lookup is a paid call under the "hunter" budget.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from sqlalchemy.orm import Session

from app.budget.gate import BudgetGate, BudgetWait, SpendEstimate
from app.budget.pricing import estimate_cost
from app.core.config import Settings
from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest
from app.models import Job, OutreachContact, SpendKind

HUNTER_FINDER = "https://api.hunter.io/v2/email-finder"
HUNTER_PROVIDER = "hunter"
# Hunter's own scale: below this the address is as good as a guess.
MIN_SCORE = 50

# Hosts that carry the job, not the employer: their domain is never the company's email domain.
_JOB_HOSTS = (
    "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "teamtailor.com", "bamboohr.com", "recruitee.com",
    "personio.", "linkedin.com", "indeed.", "google.", "reed.co.uk", "totaljobs.com", "cv-library.co.uk",
    "glassdoor.", "applytojob.com", "breezy.hr", "pinpointhq.com", "tal.net", "eploy.", "oleeo.",
    "successfactors.", "taleo.net", "jobs.ac.uk", "civilservicejobs", "nhs.uk", "gov.uk", "hirehive.",
    "jazzhr.", "rippling.", "dayforcehcm.", "adp.com", "oraclecloud.com", "zohorecruit.", "join.com",
    "welcometothejungle.", "otta.com", "cord.co", "builtin.com", "totaljobs.", "monster.", "adzuna.",
)
_SUBDOMAINS = ("www.", "careers.", "jobs.", "apply.", "join.", "work.", "recruiting.", "talent.")
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "phd", "mba", "cfa", "acca", "aca", "frsa"}
FORMATS = ("{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{f}.{last}", "{first}_{last}", "{last}")


@dataclass(slots=True)
class EmailResult:
    email: str | None = None
    source: str | None = None  # "found" (came with the contact), "hunter" or "guessed"
    score: int | None = None
    alternatives: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def company_domain(job: Job) -> str | None:
    """The employer's own web domain, from an apply link on its own site; None for a job board or ATS."""
    host = (urlsplit(job.apply_url or "").hostname or "").lower()
    if not host or "." not in host or any(marker in host for marker in _JOB_HOSTS):
        return None
    for prefix in _SUBDOMAINS:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host if host.count(".") >= 1 else None


def name_parts(full_name: str) -> tuple[str, str] | None:
    """(first, last) in plain lowercase letters, or None for a one-word or unreadable name."""
    plain = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode()
    plain = re.sub(r"\(.*?\)|,.*$", " ", plain)
    words = [re.sub(r"[^a-z-]", "", word.lower()) for word in plain.split()]
    words = [w.strip("-") for w in words if w.strip("-") and w.strip("-") not in _HONORIFICS | _SUFFIXES]
    if len(words) < 2:
        return None
    return words[0].replace("-", ""), words[-1].replace("-", "")


def guesses(full_name: str, domain: str) -> list[str]:
    parts = name_parts(full_name)
    if parts is None:
        return []
    first, last = parts
    return [f"{form.format(first=first, last=last, f=first[0])}@{domain}" for form in FORMATS]


async def find_email(
    session: Session,
    contact: OutreachContact,
    job: Job,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None = None,
) -> EmailResult:
    """The contact's email, stored on the contact with where it came from. Never replaces one already found."""
    if contact.email:
        source = str((contact.evidence or {}).get("email_source") or "found")
        return EmailResult(email=contact.email, source=source)
    domain = company_domain(job)
    candidates = guesses(contact.full_name, domain) if domain else []
    if not candidates:
        return EmailResult()
    result = EmailResult(email=candidates[0], source="guessed", alternatives=candidates[1:4])
    key = settings.hunter_api_key.strip()
    if key:
        found = await _hunter(session, contact, domain or "", key, gateway=gateway, settings=settings, persist=persist, now=now)
        if found.stopped_by:
            result.stopped_by = found.stopped_by
        elif found.email:
            result = found
    contact.email = result.email
    contact.evidence = {
        **(contact.evidence or {}),
        "email_source": result.source,
        "email_score": result.score,
        "email_alternatives": result.alternatives,
    }
    session.flush()
    persist()
    return result


async def _hunter(
    session: Session,
    contact: OutreachContact,
    domain: str,
    key: str,
    *,
    gateway: FetchGateway,
    settings: Settings,
    persist: Callable[[], None],
    now: Callable[[], datetime] | None,
) -> EmailResult:
    parts = name_parts(contact.full_name)
    if parts is None:
        return EmailResult()
    gate = BudgetGate(session, clock=now or (lambda: datetime.now(UTC)))
    cost = estimate_cost(provider=HUNTER_PROVIDER, kind=SpendKind.email_lookup, units=Decimal(1), settings=settings)
    try:
        reservation = gate.reserve(
            SpendEstimate(
                provider=HUNTER_PROVIDER,
                kind=SpendKind.email_lookup,
                units=Decimal(1),
                unit_label="email_lookup",
                estimated_cost=cost,
                related_item_id=contact.id,
            )
        )
    except BudgetWait:
        return EmailResult(stopped_by="budget")
    persist()
    query = urlencode({"domain": domain, "first_name": parts[0], "last_name": parts[1], "api_key": key})
    response = await gateway.fetch(FetchRequest(url=f"{HUNTER_FINDER}?{query}", method="GET", kind="api", timeout_s=20.0))
    if isinstance(response, FetchRefusal) or response.status_code >= 400:
        # 404 means Hunter has no address for this person; nothing was used up worth counting.
        gate.settle(reservation, consumed=False)
        persist()
        return EmailResult()
    gate.settle(reservation, actual_cost=cost, units=Decimal(1))
    persist()
    try:
        data = json.loads(response.body).get("data") or {}
    except (ValueError, AttributeError):
        return EmailResult()
    email, score = data.get("email"), data.get("score")
    if not isinstance(email, str) or "@" not in email or not isinstance(score, int) or score < MIN_SCORE:
        return EmailResult()
    return EmailResult(email=email.lower(), source="hunter", score=score)


def email_note(contact: OutreachContact) -> str:
    """How the address was found, for the person reading it before they send."""
    evidence = contact.evidence or {}
    source = evidence.get("email_source")
    if not contact.email:
        return "No email found. Use the LinkedIn note instead."
    if source == "hunter":
        return f"Found by Hunter ({evidence.get('email_score')}% confident)."
    if source == "guessed":
        others = ", ".join(evidence.get("email_alternatives") or [])
        tail = f" Other common formats: {others}." if others else ""
        return "Guessed from the company's domain in the most common format; not checked, so it may bounce." + tail
    return "Came with the contact's public details."
