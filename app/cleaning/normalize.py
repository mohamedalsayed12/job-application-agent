"""Turn one raw job into the common job format. Pure: no database, no network."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.matching.titles import clean_html

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
    company: str
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
    """None when a job has no title, company or link, or its fields are not the expected types."""
    try:
        if source == "linkedin":
            parsed = _linkedin(raw)
        elif source == "indeed":
            parsed = _indeed(raw)
        elif source == "google_jobs":
            parsed = _google_jobs(raw)
        elif source == "careers":
            parsed = _careers(raw)
        else:
            parsed = None
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed and parsed.title and parsed.company and parsed.apply_url else None


# LinkedIn names regions like "Greater Manchester Area" and "London Area, United Kingdom".
_LINKEDIN_AREA = re.compile(r"^greater\s+|\s+(?:metropolitan\s+)?area\b", re.IGNORECASE)
_INDEED_SALARY_PERIOD = {
    "YEAR": "year",
    "MONTH": "month",
    "WEEK": "week",
    "DAY": "day",
    "HOUR": "hour",
}


def _linkedin(raw: dict[str, Any]) -> NormalizedJob:
    """One item from the Apify LinkedIn job scraper. Easy Apply jobs have no outside link, so they keep LinkedIn's."""
    where = _LINKEDIN_AREA.sub("", clean_html(raw.get("location")))
    employment = (raw.get("employmentType") or "").lower()
    apply_url = raw.get("applyUrl")
    if not isinstance(apply_url, str) or not apply_url.startswith(("http://", "https://")):
        apply_url = raw.get("jobUrl") or ""
    return NormalizedJob(
        title=clean_html(raw.get("title")),
        company=clean_html(raw.get("company")),
        apply_url=apply_url,
        location=parse_location(where, workplace=raw.get("workplaceType") or raw.get("workType")),
        places=parse_places(where),
        salary=Salary(),
        description=clean_html(raw.get("description")) or clean_html(raw.get("descriptionHtml")) or None,
        hours=_hours(employment),
        contract_type=_contract_type(employment),
        posted_at=_iso_date(raw.get("postedAt")),
        closes_at=None,
    )


def _google_jobs(raw: dict[str, Any]) -> NormalizedJob:
    """One item from SerpAPI's Google Jobs engine. Prefer the first http(s) apply option."""
    where = clean_html(raw.get("location"))
    extensions = raw.get("detected_extensions") if isinstance(raw.get("detected_extensions"), dict) else {}
    schedule = (
        extensions.get("schedule_type").lower()
        if isinstance(extensions.get("schedule_type"), str)
        else ""
    )
    apply_url = ""
    options = raw.get("apply_options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            link = option.get("link")
            if isinstance(link, str) and link.startswith(("http://", "https://")):
                apply_url = link
                break
    if not apply_url:
        share = raw.get("share_link")
        if isinstance(share, str) and share.startswith(("http://", "https://")):
            apply_url = share
    remote_type = None
    if where and _REMOTE.search(where):
        remote_type = "remote"
    elif where and _HYBRID.search(where):
        remote_type = "hybrid"
    return NormalizedJob(
        title=clean_html(raw.get("title")),
        company=clean_html(raw.get("company_name")),
        apply_url=apply_url,
        location=parse_location(where, workplace=remote_type),
        places=parse_places(where),
        salary=Salary(),
        description=clean_html(raw.get("description")) or None,
        hours=_hours(schedule),
        contract_type=_contract_type(schedule),
        posted_at=None,
        closes_at=None,
    )


def _indeed(raw: dict[str, Any]) -> NormalizedJob:
    """One item from Apify valig/indeed-jobs-scraper. Prefer the employer apply link when Indeed provides one."""
    employer = raw.get("employer") if isinstance(raw.get("employer"), dict) else {}
    place = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    city = clean_html(place.get("city")) if isinstance(place.get("city"), str) else ""
    country_code = place.get("countryCode") or place.get("countryName")
    country = None
    if isinstance(country_code, str) and country_code.strip():
        country = _COUNTRIES.get(country_code.strip().lower(), country_code.strip().upper())
    labels = _indeed_labels(raw)
    label_text = " ".join(labels)
    remote_type = _indeed_workplace(labels)
    if city and _REMOTE.search(city):
        remote_type = remote_type or "remote"
        city = ""
    apply_url = raw.get("jobUrl")
    if not isinstance(apply_url, str) or not apply_url.startswith(("http://", "https://")):
        apply_url = raw.get("url") or ""
    description = raw.get("description")
    if isinstance(description, dict):
        text = clean_html(description.get("text")) or clean_html(description.get("html")) or None
    else:
        text = clean_html(description) or None
    city_value = city or None
    return NormalizedJob(
        title=clean_html(raw.get("title")),
        company=clean_html(employer.get("name")),
        apply_url=apply_url,
        location=Location(city=city_value, country=country, remote_type=remote_type),
        places=(Place(city=city_value, country=country),) if city_value or country else (),
        salary=_indeed_salary(raw.get("baseSalary")),
        description=text,
        hours=_hours(label_text),
        contract_type=_contract_type(label_text),
        posted_at=_iso_date(raw.get("datePublished") or raw.get("dateOnIndeed")),
        closes_at=_iso_date(raw.get("expirationDate")),
    )


def _indeed_labels(raw: dict[str, Any]) -> list[str]:
    """Attribute and job-type labels from the actor, kept as separate values (no key overwrite)."""
    labels: list[str] = []
    for field_name in ("attributes", "jobTypes"):
        values = raw.get(field_name)
        if not isinstance(values, dict):
            continue
        for value in values.values():
            text = str(value).strip().lower()
            if text:
                labels.append(text)
    return labels


_INDEED_REMOTE = frozenset({"remote", "remote work", "work from home", "home-based", "home based"})
_INDEED_HYBRID = frozenset({"hybrid", "hybrid work"})


def _indeed_workplace(labels: list[str]) -> str | None:
    if any(label in _INDEED_REMOTE for label in labels):
        return "remote"
    if any(label in _INDEED_HYBRID for label in labels):
        return "hybrid"
    return None


def _indeed_salary(raw: object) -> Salary:
    if not isinstance(raw, dict):
        return Salary()
    period = _INDEED_SALARY_PERIOD.get(str(raw.get("unitOfWork") or "").upper())
    currency = raw.get("currencyCode")
    if not isinstance(currency, str) or not currency.strip():
        currency = None
    else:
        currency = currency.strip().upper()
    return Salary(
        minimum=_money(raw.get("min")),
        maximum=_money(raw.get("max")),
        currency=currency,
        period=period,
    )


def _careers(raw: dict[str, Any]) -> NormalizedJob:
    """A schema.org JobPosting read from an employer's own careers page (D69). Its page is the apply link."""
    posting = raw.get("posting") if isinstance(raw.get("posting"), dict) else {}
    org = posting.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else org
    remote = "remote" if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE" else None
    where = "; ".join(_schema_places(posting.get("jobLocation")))
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = " ".join(str(item) for item in employment)
    commitment = str(employment or "").lower().replace("_", " ")
    return NormalizedJob(
        title=clean_html(posting.get("title") if isinstance(posting.get("title"), str) else ""),
        company=clean_html(company if isinstance(company, str) else ""),
        apply_url=raw.get("page_url") if isinstance(raw.get("page_url"), str) else "",
        location=parse_location(where, workplace=remote),
        places=parse_places(where),
        salary=_schema_salary(posting.get("baseSalary")),
        description=clean_html(posting.get("description") if isinstance(posting.get("description"), str) else "")
        or None,
        hours=_hours(commitment),
        contract_type=_contract_type(commitment),
        posted_at=_iso_date(posting.get("datePosted")),
        closes_at=_iso_date(posting.get("validThrough")),
    )


def _schema_places(value: object) -> list[str]:
    """'City, Country' for each schema.org Place; the address may be text or a PostalAddress."""
    places = value if isinstance(value, list) else [value]
    found = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, str):
            found.append(clean_html(address))
            continue
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [address.get("addressLocality"), country]
        text = ", ".join(clean_html(part) for part in parts if isinstance(part, str) and part.strip())
        if text:
            found.append(text)
    return found


def _schema_salary(raw: object) -> Salary:
    """schema.org MonetaryAmount: a currency and a QuantitativeValue with a value or a min and max."""
    if not isinstance(raw, dict):
        return Salary()
    amount = raw.get("value")
    currency = raw.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None
    if not isinstance(amount, dict):
        return Salary(minimum=_money(amount), maximum=_money(amount), currency=currency) if _money(amount) else Salary()
    period = _INDEED_SALARY_PERIOD.get(str(amount.get("unitText") or "").upper())
    low = _money(amount.get("minValue"))
    high = _money(amount.get("maxValue"))
    if low is None and high is None:
        low = high = _money(amount.get("value"))
    return Salary(minimum=low, maximum=high, currency=currency, period=period)


def _money(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


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
    """Turn place shorthand such as 'US-SF-HQ' or 'CA-Toronto' into the city and country it means."""
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
