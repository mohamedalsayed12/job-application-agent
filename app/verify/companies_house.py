"""Companies House Public Data API lookups through the fetch gateway (D55)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.fetch.gateway import FetchGateway, FetchRefusal, FetchRequest, FetchResponse

CH_SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"
# Statuses that mean the legal entity is not trading as a normal employer.
DEAD_STATUSES = frozenset(
    {
        "dissolved",
        "liquidation",
        "receivership",
        "administration",
        "voluntary-arrangement",
        "converted-closed",
        "insolvency-proceedings",
        "removed",
        "closed",
    }
)
ACTIVE_STATUSES = frozenset({"active", "registered"})


@dataclass(frozen=True, slots=True)
class CompanyHit:
    company_number: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class CompanyLookup:
    hit: CompanyHit | None
    reason: str
    evidence: dict[str, Any]


def _auth_header(api_key: str) -> dict[str, str]:
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


async def lookup_company(
    gateway: FetchGateway,
    company_name: str,
    *,
    api_key: str,
) -> CompanyLookup:
    """Find one active company by name, or mark dissolved / unknown."""
    if not api_key.strip():
        return CompanyLookup(None, "companies_house_key_missing", {})
    name = company_name.strip()
    if not name:
        return CompanyLookup(None, "company_name_empty", {})

    url = f"{CH_SEARCH_URL}?q={quote(name)}&items_per_page=5"
    outcome = await gateway.fetch(
        FetchRequest(url=url, kind="api", headers=_auth_header(api_key), timeout_s=15.0)
    )
    if isinstance(outcome, FetchRefusal):
        return CompanyLookup(None, f"companies_house_fetch:{outcome.reason}", {})
    if not isinstance(outcome, FetchResponse) or outcome.status_code != 200:
        code = getattr(outcome, "status_code", "?")
        return CompanyLookup(None, f"companies_house_http_{code}", {})

    try:
        data = json.loads(outcome.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return CompanyLookup(None, "companies_house_bad_json", {})

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return CompanyLookup(None, "company_not_on_register", {"query": name})

    parsed = [_item(row) for row in items]
    parsed = [hit for hit in parsed if hit is not None]
    if not parsed:
        return CompanyLookup(None, "company_not_on_register", {"query": name})

    exact = [hit for hit in parsed if hit.title.casefold() == name.casefold()]
    # Only an exact name match can mark the firm dead (fuzzy dissolved hits must not hide real jobs).
    if exact:
        hit = exact[0]
        if hit.status in DEAD_STATUSES:
            return CompanyLookup(hit, "company_not_trading", {"query": name, "status": hit.status})
        if hit.status in ACTIVE_STATUSES:
            return CompanyLookup(hit, "company_active", {"query": name, "matches": len(parsed)})
        return CompanyLookup(hit, "company_status_unclear", {"query": name, "status": hit.status})
    alive = [hit for hit in parsed if hit.status in ACTIVE_STATUSES]
    if alive:
        hit = alive[0]
        return CompanyLookup(hit, "company_active", {"query": name, "matches": len(parsed), "fuzzy": True})
    return CompanyLookup(None, "company_not_on_register", {"query": name, "matches": len(parsed)})


def _item(row: object) -> CompanyHit | None:
    if not isinstance(row, dict):
        return None
    number = row.get("company_number")
    title = row.get("title")
    status = row.get("company_status") or ""
    if not isinstance(number, str) or not isinstance(title, str) or not number.strip():
        return None
    return CompanyHit(number.strip(), title.strip(), str(status).strip().lower())
