"""Turn one raw board job into the common job format. Pure: no database, no network."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.matching.entry_level import clean_html

_COUNTRIES = {
    "uk": "GB",
    "gb": "GB",
    "united kingdom": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "ireland": "IE",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "canada": "CA",
    "india": "IN",
    "singapore": "SG",
    "china": "CN",
    "taiwan": "TW",
    "france": "FR",
    "germany": "DE",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "australia": "AU",
    "japan": "JP",
    "portugal": "PT",
    "poland": "PL",
    "latvia": "LV",
    "lithuania": "LT",
    "estonia": "EE",
    "united arab emirates": "AE",
    "uae": "AE",
    "saudi arabia": "SA",
    "qatar": "QA",
    "kuwait": "KW",
    "belgium": "BE",
    "bulgaria": "BG",
    "romania": "RO",
    "mexico": "MX",
    "brazil": "BR",
    "argentina": "AR",
    "colombia": "CO",
    "chile": "CL",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "switzerland": "CH",
    "austria": "AT",
    "luxembourg": "LU",
    "greece": "GR",
    "turkey": "TR",
    "israel": "IL",
    "egypt": "EG",
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "pakistan": "PK",
    "new zealand": "NZ",
    "south korea": "KR",
    "hong kong": "HK",
    "malaysia": "MY",
    "thailand": "TH",
    "indonesia": "ID",
    "philippines": "PH",
    "vietnam": "VN",
    "czech republic": "CZ",
    "czechia": "CZ",
    "hungary": "HU",
    "serbia": "RS",
    "croatia": "HR",
    "slovakia": "SK",
    "slovenia": "SI",
    "ukraine": "UA",
    "cyprus": "CY",
    "malta": "MT",
    "iceland": "IS",
}
_US_STATES = frozenset(
    {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
        "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
        "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
        "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
        "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    }
)
_REGIONS = frozenset({"emea", "apac", "americas", "worldwide", "global", "europe", "nationwide", "n/a", "na"})
_REMOTE = re.compile(r"\b(remote|home[- ]based|work from home)\b", re.IGNORECASE)
_OFFICE_PREFIX = re.compile(r"^office[- ]based\s*-\s*", re.IGNORECASE)
_ISO_SUFFIX = re.compile(r"\s*\(([A-Za-z]{2})\)\s*$")
_EMPLOYER_SUFFIX = re.compile(r"\s+-\s+.*$")
_MANY_PLACES = re.compile(r";|/|\s(?:or|and)\s", re.IGNORECASE)
_US_STATE = re.compile(r"^[A-Z]{2}$")

_REMOTE_TYPES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
_MONTHS_PER_PERIOD = {"year": 1, "month": 12, "week": 52}
_SALARY_INTERVAL = re.compile(r"^per-(year|month|week|day|hour)-")


@dataclass(frozen=True, slots=True)
class Salary:
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str | None = None
    period: str | None = None

    @property
    def known(self) -> bool:
        return self.minimum is not None or self.maximum is not None


@dataclass(frozen=True, slots=True)
class Location:
    city: str | None = None
    country: str | None = None
    remote_type: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    title: str
    apply_url: str
    location: Location
    salary: Salary
    description: str | None
    hours: str | None
    contract_type: str | None
    posted_at: datetime | None
    closes_at: datetime | None


def normalize_job(source: str, raw: dict[str, Any]) -> NormalizedJob | None:
    """None when the job has no title or apply link, because neither can be invented."""
    parsed = _greenhouse(raw) if source == "greenhouse" else _lever(raw) if source == "lever" else None
    return parsed if parsed and parsed.title and parsed.apply_url else None


def _greenhouse(raw: dict[str, Any]) -> NormalizedJob:
    name = clean_html((raw.get("location") or {}).get("name"))
    return NormalizedJob(
        title=clean_html(raw.get("title")),
        apply_url=raw.get("absolute_url") or "",
        location=parse_location(name),
        salary=Salary(),
        description=clean_html(raw.get("content")) or None,
        hours=None,
        contract_type=None,
        posted_at=_iso_date(raw.get("first_published")),
        closes_at=_iso_date(raw.get("application_deadline")),
    )


def _lever(raw: dict[str, Any]) -> NormalizedJob:
    categories = raw.get("categories") or {}
    commitment = (categories.get("commitment") or "").lower()
    # Lever's description is the opening plus the body, so it is used instead of them, not as well.
    main = clean_html(raw.get("description")) or " ".join(
        part for part in (clean_html(raw.get("opening")), clean_html(raw.get("descriptionBody"))) if part
    )
    sections = [main, *(clean_html(part.get("content")) for part in raw.get("lists") or [])]
    sections.append(clean_html(raw.get("additional")))
    return NormalizedJob(
        title=clean_html(raw.get("text")),
        apply_url=raw.get("applyUrl") or raw.get("hostedUrl") or "",
        location=parse_location(
            clean_html(categories.get("location")),
            country=raw.get("country"),
            workplace=raw.get("workplaceType"),
        ),
        salary=parse_lever_salary(raw.get("salaryRange")),
        description=" ".join(part for part in sections if part) or None,
        hours=_hours(commitment),
        contract_type=_contract_type(commitment),
        posted_at=_epoch_ms(raw.get("createdAt")),
        closes_at=None,
    )


def parse_location(text: str, *, country: str | None = None, workplace: str | None = None) -> Location:
    """A city is recorded only when the text names exactly one place."""
    remote_type = _REMOTE_TYPES.get((workplace or "").lower())
    if _REMOTE.search(text):
        remote_type = "remote"

    iso = _ISO_SUFFIX.search(text)
    if iso:
        country = country or _COUNTRIES.get(iso.group(1).lower(), iso.group(1))
    place = _ISO_SUFFIX.sub("", _OFFICE_PREFIX.sub("", text)).strip()
    place = _EMPLOYER_SUFFIX.sub("", place).strip()
    resolved = country.upper() if country else None

    if not place or _REMOTE.search(place) or _MANY_PLACES.search(place):
        return Location(country=resolved or _mentioned_country(text), remote_type=remote_type)

    parts = [part.strip() for part in place.split(",") if part.strip()]
    last = parts[-1] if parts else ""
    named = _COUNTRIES.get(last.lower())
    if len(parts) == 1:
        if named:
            return Location(country=resolved or named, remote_type=remote_type)
        if last.lower() in _REGIONS:
            return Location(country=resolved, remote_type=remote_type)
        return Location(city=last, country=resolved, remote_type=remote_type)
    if named and len(parts) <= 3:
        return Location(city=parts[0], country=resolved or named, remote_type=remote_type)
    if len(parts) == 2 and (_US_STATE.match(last) or last.lower() in _US_STATES):
        return Location(city=parts[0], country=resolved or "US", remote_type=remote_type)
    return Location(country=resolved or _mentioned_country(place), remote_type=remote_type)


def _mentioned_country(text: str) -> str | None:
    """The country, when every country named in a multi-place location is the same one."""
    lowered = text.lower()
    found = {code for name, code in _COUNTRIES.items() if re.search(rf"\b{re.escape(name)}\b", lowered)}
    return found.pop() if len(found) == 1 else None


def parse_lever_salary(raw: Any) -> Salary:
    """Anything missing, zero or in an unrecognised format is unknown, never zero."""
    if not isinstance(raw, dict):
        return Salary()
    interval = _SALARY_INTERVAL.match(str(raw.get("interval") or ""))
    minimum, maximum = _positive(raw.get("min")), _positive(raw.get("max"))
    if interval is None or (minimum is None and maximum is None):
        return Salary()
    period = interval.group(1)
    factor = _MONTHS_PER_PERIOD.get(period)
    if factor is not None:
        minimum = minimum * factor if minimum is not None else None
        maximum = maximum * factor if maximum is not None else None
        period = "year"
    return Salary(minimum=minimum, maximum=maximum, currency=raw.get("currency"), period=period)


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _hours(commitment: str) -> str | None:
    if "full" in commitment:
        return "full_time"
    if "part" in commitment:
        return "part_time"
    return None


def _contract_type(commitment: str) -> str | None:
    if "permanent" in commitment:
        return "permanent"
    if "fixed" in commitment:
        return "fixed_term"
    if "consultant" in commitment or "contractor" in commitment:
        return "contractor"
    return None


def _iso_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _epoch_ms(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
