"""D34: every place a job is listed for, read from its location text."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.cleaning.normalize import Place, normalize_job, parse_places
from tests.jobs import _job, _li, _store

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
        ("Global", (P(region="worldwide"),)),
        ("EMEA", (P(region="emea"),)),
        ("Home based - Worldwide", (P(region="worldwide"),)),
        ("Home Based - Americas; Home Based - APAC; Home based - EMEA", (P(region="americas"), P(region="apac"), P(region="emea"))),
        ("Home Based - APAC", (P(region="apac"),)),
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


def test_a_linkedin_location_names_its_city_and_country() -> None:
    assert parse_places("Manchester, England, United Kingdom") == (P("Manchester", "GB"),)
    assert normalize_job("linkedin", _li(1, city="Greater Manchester Area")).places == (P("Manchester"),)
    assert normalize_job("linkedin", _li(1, city="London Area, United Kingdom")).places == (P("London", "GB"),)


def test_places_reach_the_posting_and_the_job_and_the_single_city_stays_as_it_was(session: Session) -> None:
    _store(session, [_li(1, city="London, UK; Manchester, UK")])
    job = _job(session, 1)
    assert job.places == [
        {"city": "London", "country": "GB", "region": None},
        {"city": "Manchester", "country": "GB", "region": None},
    ]
    assert job.location_city is None
    assert job.location_country == "GB"
