"""D34: every place a job is listed for, read from the location text boards really send."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.cleaning.normalize import Place, normalize_job, parse_places
from tests.test_cleaning import _board, _Collector, _job

P = Place


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Manchester, UK", (P("Manchester", "GB"),)),
        ("Chicago, IL", (P("Chicago"),)),
        ("Atlanta, GA", (P("Atlanta", "US"),)),
        ("Toronto, Ontario, Canada", (P("Toronto", "CA"),)),
        ("London, Dublin", (P("London"), P("Dublin"))),
        ("Dublin or London", (P("Dublin"), P("London"))),
        ("Dublin, London OR Berlin", (P("Dublin"), P("London"), P("Berlin"))),
        ("London, UK; Dublin, Ireland", (P("London", "GB"), P("Dublin", "IE"))),
        ("Chicago, IL; Atlanta, GA", (P("Chicago"), P("Atlanta", "US"))),
        ("Seattle, WA, New York, NY", (P("Seattle", "US"), P("New York", "US"))),
        ("Seattle, New York", (P("Seattle"), P("New York"))),
        ("Dublin, Ireland, London, UK", (P("Dublin", "IE"), P("London", "GB"))),
        ("US / Canada", (P(country="US"), P(country="CA"))),
        ("US, Canada", (P(country="US"), P(country="CA"))),
        ("United Kingdom; Ireland", (P(country="GB"), P(country="IE"))),
        ("Poland - Remote OR Romania - Remote", (P(country="PL"), P(country="RO"))),
        ("SF, NYC, CHI, ATL, SEA", (P("San Francisco", "US"), P("New York", "US"), P("Chicago", "US"), P("Atlanta", "US"), P("Seattle", "US"))),
        ("SF, NY, Remote", (P("San Francisco", "US"), P("New York", "US"))),
        ("NYC, Sea, SF, Tor", (P("New York", "US"), P("Seattle", "US"), P("San Francisco", "US"), P("Toronto", "CA"))),
        ("US-Chicago, US-Remote", (P("Chicago", "US"), P(country="US"))),
        ("CA-Toronto", (P("Toronto", "CA"),)),
        ("IN-Bengaluru", (P("Bengaluru", "IN"),)),
        ("US-SF-HQ", (P("San Francisco", "US"),)),
        ("South San Francisco HQ", (P("South San Francisco"),)),
        ("NYC-Privy", (P("New York", "US"),)),
        ("Germany (Berlin/Munich)", (P("Berlin", "DE"), P("Munich"))),
        ("Cardiff, London or Remote (UK)", (P("Cardiff"), P("London"), P(country="GB"))),
        ("London, England; London, United Kingdom", (P("London", "GB"),)),
        ("London, Manchester, London, UK", (P("London", "GB"), P("Manchester"))),
        ("Deliveroo; London, United Kingdom", (P("London", "GB"),)),
        ("London, United Kingdom - Deliveroo", (P("London", "GB"),)),
        ("Stoke-on-Trent, UK", (P("Stoke-on-Trent", "GB"),)),
        ("Remote", ()),
        ("N/A", ()),
        ("na", ()),
        ("Global", ()),
        ("EMEA", ()),
        ("Deliveroo", ()),
        ("In-person", ()),
        ("", ()),
    ],
)
def test_places_named_by_location_text(text: str, expected: tuple[Place, ...]) -> None:
    assert parse_places(text) == expected


def test_a_given_country_applies_to_one_place_only() -> None:
    assert parse_places("Manchester", country="gb") == (P("Manchester", "GB"),)
    assert parse_places("Leeds; York", country="gb") == (P("Leeds"), P("York"))


LEVER = {
    "text": "Engineer",
    "applyUrl": "https://jobs.lever.co/acme/1",
    "country": "GB",
}


def test_lever_uses_its_list_of_locations_when_there_is_more_than_one() -> None:
    raw = {**LEVER, "categories": {"location": "London (GB)", "allLocations": ["London (GB)", "Remote (FR)", "Paris (FR)"]}}
    assert normalize_job("lever", raw).places == (P("London", "GB"), P(country="FR"), P("Paris", "FR"))


def test_lever_with_one_location_uses_its_country() -> None:
    raw = {**LEVER, "categories": {"location": "Manchester", "allLocations": ["Manchester"]}}
    assert normalize_job("lever", raw).places == (P("Manchester", "GB"),)


def test_odd_lever_location_lists_do_not_break_a_job() -> None:
    raw = {**LEVER, "categories": {"location": "Manchester", "allLocations": "Manchester"}}
    assert normalize_job("lever", raw).places == (P("Manchester", "GB"),)


async def test_places_reach_the_posting_and_the_job_and_the_single_city_stays_as_it_was(session: Session) -> None:
    _board(session)
    collector = _Collector(session)
    await collector.read(
        200,
        {
            "jobs": [
                {
                    "id": 1,
                    "title": "Backend Developer",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                    "location": {"name": "London, UK; Manchester, UK"},
                    "first_published": "2026-11-01T10:00:00+00:00",
                    "content": "Build APIs.",
                }
            ]
        },
    )
    job = _job(session, 1)
    assert job.places == [{"city": "London", "country": "GB"}, {"city": "Manchester", "country": "GB"}]
    assert job.location_city is None
    assert job.location_country == "GB"
