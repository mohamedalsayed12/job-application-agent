"""Turn one raw board job into the common job format. Pure: no database, no network."""

from __future__ import annotations

import re
from collections.abc import Iterable
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
    "united states of america": "US",
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
        "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
        "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
        "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
        "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    }
)
_US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DC DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
_CANADIAN_PROVINCES = frozenset({"AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK"})
_COUNTRY_CODES = frozenset(_COUNTRIES.values())
_NOT_PLACES = frozenset(
    {
        "emea", "apac", "americas", "worldwide", "global", "europe", "nationwide", "n/a", "na",
        "flexible", "anywhere", "various", "multiple locations", "tbc", "tbd",
    }
)
_REMOTE = re.compile(r"\b(remote|home[- ]based|work from home)\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_WORK_MODE = re.compile(r"\(?\b(?:hybrid|on-?site|in[- ]office)\b\)?", re.IGNORECASE)
_OFFICE_PREFIX = re.compile(r"^office[- ]based\s*-\s*", re.IGNORECASE)
_ISO_SUFFIX = re.compile(r"\s*\(([A-Za-z]{2})\)\s*$")
_MANY_PLACES = re.compile(r";|/|\s(?:or|and)\s", re.IGNORECASE)
_PLACE_SEPARATOR = re.compile(r";|/|\||&|\s(?:or|and)\s", re.IGNORECASE)
_NOT_APPLICABLE = re.compile(r"\bn/a\b", re.IGNORECASE)
_CITY_SHORT_NAMES = {
    "sf": ("San Francisco", "US"),
    "nyc": ("New York", "US"),
    "ny": ("New York", "US"),
    "new-york": ("New York", "US"),
    "new york city": ("New York", "US"),
    "chi": ("Chicago", "US"),
    "atl": ("Atlanta", "US"),
    "sea": ("Seattle", "US"),
    "tor": ("Toronto", "CA"),
    "toro": ("Toronto", "CA"),
    "dc": ("Washington", "US"),
}
# Also the names of cities, so on their own in a list they are places, not regions.
_CITY_AND_STATE = frozenset({"new york", "washington"})
_COUNTRY_PREFIX = re.compile(r"^([A-Za-z ]+?)\s*[-:(]\s*(.+)$")
_CITY_SUFFIX = re.compile(r"[- ]+(?:HQ|Privy)\b", re.IGNORECASE)
_NOT_CITIES = frozenset({"rem", "remote", "in-person", "deliveroo"})
_REGIONS = {
    "emea": "emea",
    "europe": "europe",
    "apac": "apac",
    "americas": "americas",
    "worldwide": "worldwide",
    "global": "worldwide",
}
_REGION = re.compile(rf"\b({'|'.join(_REGIONS)})\b", re.IGNORECASE)

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
class Place:
    city: str | None = None
    country: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    title: str
    apply_url: str
    location: Location
    places: tuple[Place, ...]
    salary: Salary
    description: str | None
    hours: str | None
    contract_type: str | None
    posted_at: datetime | None
    closes_at: datetime | None


def normalize_job(source: str, raw: dict[str, Any]) -> NormalizedJob | None:
    """None when a job has no title or apply link, or its fields are not the expected types."""
    try:
        parsed = _greenhouse(raw) if source == "greenhouse" else _lever(raw) if source == "lever" else None
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed and parsed.title and parsed.apply_url else None


_OFFICE_SUFFIX = re.compile(r"\s+locations?$", re.IGNORECASE)


def _greenhouse_where(raw: dict[str, Any]) -> tuple[Location, tuple[Place, ...]]:
    """The location field, or the offices when it holds no place: "N/A", "In-person", the company's name."""
    name = clean_html((raw.get("location") or {}).get("name"))
    places = parse_places(name)
    if places:
        return parse_location(name), places
    offices = [
        _OFFICE_SUFFIX.sub("", clean_html(office.get("name")))
        for office in raw.get("offices") or []
        if isinstance(office, dict) and office.get("name")
    ]
    from_offices = tuple(dict.fromkeys(place for office in offices for place in parse_places(office)))
    if not from_offices:
        return parse_location(name), ()
    first = from_offices[0]
    return Location(city=first.city, country=first.country, remote_type=parse_location(name).remote_type), from_offices


def _greenhouse(raw: dict[str, Any]) -> NormalizedJob:
    location, places = _greenhouse_where(raw)
    return NormalizedJob(
        title=clean_html(raw.get("title")),
        apply_url=raw.get("absolute_url") or "",
        location=location,
        places=places,
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
        places=_lever_places(raw, categories),
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
    elif remote_type is None and _HYBRID.search(text):
        remote_type = "hybrid"

    place = _WORK_MODE.sub("", text).strip(" -,")
    iso = _ISO_SUFFIX.search(place)
    if iso:
        country = country or _COUNTRIES.get(iso.group(1).lower(), iso.group(1))
    place = _ISO_SUFFIX.sub("", _OFFICE_PREFIX.sub("", place)).strip()
    place = _undash(place)
    resolved = country.upper() if country else None

    if not place or _REMOTE.search(place) or _MANY_PLACES.search(place):
        return Location(country=resolved or _mentioned_country(text), remote_type=remote_type)

    parts = [part.strip() for part in place.split(",") if part.strip()]
    last = parts[-1] if parts else ""
    named = _COUNTRIES.get(last.lower())
    if len(parts) == 1:
        if named:
            return Location(country=resolved or named, remote_type=remote_type)
        if last.lower() in _NOT_PLACES:
            return Location(country=resolved, remote_type=remote_type)
        return Location(city=last, country=resolved, remote_type=remote_type)
    if named and len(parts) <= 3:
        return Location(city=parts[0], country=resolved or named, remote_type=remote_type)
    if len(parts) == 2 and last.lower() in _US_STATES:
        return Location(city=parts[0], country=resolved or "US", remote_type=remote_type)
    if len(parts) == 2 and _is_region_code(last):
        return Location(city=parts[0], country=resolved or _country_from_code(last), remote_type=remote_type)
    return Location(country=resolved or _mentioned_country(place), remote_type=remote_type)


def parse_places(text: str, *, country: str | None = None) -> tuple[Place, ...]:
    """Every place the location text names, in order. Regions and remote work name no place."""
    segments = [segment for segment in _PLACE_SEPARATOR.split(_NOT_APPLICABLE.sub("", text)) if segment.strip()]
    if len(segments) == 1:
        found = _segment_places(segments[0], country)
    else:
        found = [place for segment in segments for place in _segment_places(segment, None)]
    return _distinct(found)


def _lever_places(raw: dict[str, Any], categories: dict[str, Any]) -> tuple[Place, ...]:
    """Lever's raw country belongs to the main location only, so a list of locations does not use it."""
    listed = categories.get("allLocations")
    if isinstance(listed, list) and len(listed) > 1:
        return _distinct(place for text in listed for place in parse_places(clean_html(text)))
    return parse_places(clean_html(categories.get("location")), country=raw.get("country"))


def _segment_places(segment: str, country: str | None) -> list[Place]:
    parts = [part.strip() for part in _WORK_MODE.sub("", segment).split(",") if part.strip()]
    if len(parts) > 1 and (parts[0].lower() in _COUNTRIES or parts[-1].lower() in _CITY_AND_STATE):
        return _comma_places(parts)
    location = parse_location(segment, country=country)
    if location.city:
        return [Place(location.city, location.country)]
    if len(parts) > 1:
        return _comma_places(parts)
    if location.country:
        return [Place(country=location.country)]
    region = _REGION.search(segment)
    return [Place(region=_REGIONS[region.group(1).lower()])] if region else []


def _comma_places(parts: list[str]) -> list[Place]:
    """'Seattle, WA, New York, NY' is two places: a region belongs to the city just before it."""
    places: list[Place] = []
    for part in parts:
        lowered = part.lower()
        named = _COUNTRIES.get(lowered)
        code = _is_region_code(part)
        if named or code or (lowered in _US_STATES and lowered not in _CITY_AND_STATE):
            previous = places[-1] if places else None
            if (
                previous
                and previous.city
                and previous.country is None
                and previous.city.lower() not in _CITY_SHORT_NAMES
            ):
                region = named or ("US" if not code else _country_from_code(part))
                places[-1] = Place(previous.city, region)
                continue
            if code and not named and lowered not in _CITY_SHORT_NAMES:
                continue
        location = parse_location(part)
        if location.city or location.country:
            places.append(Place(location.city, location.country))
    return places


def _distinct(places: Iterable[Place]) -> tuple[Place, ...]:
    """One entry per place. The same city with and without a country is one place."""
    found: list[Place] = []
    for place in map(_tidy, places):
        if not (place.city or place.country or place.region):
            continue
        for index, seen in enumerate(found):
            if _same_place(seen, place):
                found[index] = seen if seen.country else place
                break
        else:
            found.append(place)
    return tuple(found)


def _same_place(first: Place, second: Place) -> bool:
    if (first.city or "").lower() != (second.city or "").lower() or first.region != second.region:
        return False
    return first.country == second.country or (
        first.city is not None and (first.country is None or second.country is None)
    )


def _tidy(place: Place) -> Place:
    """Turn board shorthand such as 'US-SF-HQ' or 'CA-Toronto' into the city and country it means."""
    if place.city is None:
        return place
    city, country = _CITY_SUFFIX.sub("", place.city).strip(" ()"), place.country
    prefix = _COUNTRY_PREFIX.match(city)
    if prefix:
        label = prefix.group(1)
        region = _COUNTRIES.get(label.lower()) or (label if label in _COUNTRY_CODES else None)
        if region:
            city, country = prefix.group(2).strip(" ()"), region
    short = _CITY_SHORT_NAMES.get(city.lower())
    if short:
        return Place(*short)
    if city.lower() in _NOT_CITIES or _is_region_code(city):
        return Place(country=country)
    return Place(city, country)


def _undash(place: str) -> str:
    """Read 'A - B' only when one side is a country. Otherwise the place is unclear, so no city."""
    if " - " not in place:
        return place
    left, right = (part.strip() for part in place.split(" - ", 1))
    if right.lower() in _COUNTRIES:
        return f"{left}, {right}"
    if left.lower() in _COUNTRIES:
        return f"{right}, {left}"
    if left.rsplit(",", 1)[-1].strip().lower() in _COUNTRIES:
        return left
    return ""


def _is_region_code(text: str) -> bool:
    return text in _US_STATE_CODES or text in _CANADIAN_PROVINCES or text in _COUNTRY_CODES


def _country_from_code(code: str) -> str | None:
    """IN could be Indiana or India, and CA California or Canada, so those record no country."""
    if code in _US_STATE_CODES:
        return None if code in _COUNTRY_CODES else "US"
    return "CA" if code in _CANADIAN_PROVINCES else code


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
