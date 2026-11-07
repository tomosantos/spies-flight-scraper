"""Unit tests. Nothing here touches the network: the API layer is monkeypatched."""
import json
from datetime import datetime, timezone

import pytest

import scraper


def make_offer(time="06:10", day="20270105", price=1000, dest="PMI", carrier="flight|DK", key="k1"):
    return {
        "meta": {"departureCode": "CPH", "destinationCode": dest},
        "includedTransportOffer": {"rawTotalPrice": price, "transportDetailsKey": key},
        "out": {"carrier": {"key": carrier}, "departure": {"date": {"url": day, "time": time}}},
    }


def make_details(raw="02:30:00", arrival="09:40", legs=1):
    leg = {
        "__typename": "BookinghubFlightLegV2",
        "legDetails": {"duration": {"raw": raw}},
        "destination": {"date": {"time": arrival}},
    }
    layover = {"__typename": "BookinghubLayoverV2"}
    return {"q2Transport": {"transport": {"out": {"flightDetails": {"legs": [leg] + [layover, leg] * (legs - 1)}}}}}


@pytest.fixture(autouse=True)
def fixed_rate(monkeypatch):
    monkeypatch.setattr(scraper, "dkk_to_eur", lambda: 0.1)


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "ROUTES_FILE", str(tmp_path / "routes.json"))
    monkeypatch.setattr(scraper, "RESULTS_FILE", str(tmp_path / "results.json"))
    return tmp_path


def freeze_now(monkeypatch, iso="2027-01-01T12:00:00+00:00"):
    fixed = datetime.fromisoformat(iso)
    monkeypatch.setattr(scraper, "now", lambda: fixed)
    return fixed


# --- carrier_code ------------------------------------------------------------

@pytest.mark.parametrize("key, expected", [
    ("flight|DK", "DK"),
    ("flight|D8", "D8"),
    ("flight|JTD", "JP"),   # ICAO mapped to IATA
    ("flight|CAT", None),   # no IATA code exists
])
def test_carrier_code(key, expected):
    assert scraper.carrier_code(key) == expected


# --- build_flight ------------------------------------------------------------

def test_build_flight_matches_expected_format():
    flight = scraper.build_flight(make_offer(), make_details())
    assert flight == {
        "departure": "2027-01-05T06:10:00",
        "arrival": "2027-01-05T09:40:00",
        "duration": 2.5,
        "carrier": "DK",
        "iata_origin": "CPH",
        "iata_destination": "PMI",
        "price": 100.0,  # 1000 DKK * 0.1
        "stops": 0,
        "layover_info": None,
        "flight_numbers": None,
        "vehicle_type": "aircraft",
    }


def test_arrival_rolls_over_to_next_day():
    # 22:00 + 3h = 01:00 the next day, destination clock shows 02:00 (one hour ahead)
    flight = scraper.build_flight(make_offer(time="22:00"), make_details(raw="03:00:00", arrival="02:00"))
    assert flight["arrival"] == "2027-01-06T02:00:00"


def test_arrival_with_timezone_gap_stays_on_same_day():
    # 10:00 + 4h05 = 14:05 local origin time, destination clock is one hour ahead
    flight = scraper.build_flight(make_offer(time="10:00"), make_details(raw="04:05:00", arrival="15:05"))
    assert flight["arrival"] == "2027-01-05T15:05:00"


def test_connecting_flight_is_dropped():
    assert scraper.build_flight(make_offer(), make_details(legs=2)) is None


def test_missing_flight_details_is_dropped():
    details = {"q2Transport": {"transport": {"out": {"flightDetails": None}}}}
    assert scraper.build_flight(make_offer(), details) is None


# --- get_update_after --------------------------------------------------------

@pytest.mark.parametrize("date, low, high", [
    ("2027-01-03", 5.4, 6.6),     # within a week: ~6h
    ("2027-01-20", 10.8, 13.2),   # within a month: ~12h
    ("2027-06-01", 21.6, 26.4),   # far away: ~24h
])
def test_update_after_depends_on_how_close_the_flight_is(monkeypatch, date, low, high):
    base = freeze_now(monkeypatch)
    route = {"origin": "CPH", "destination": "PMI", "date": date}
    hours = (datetime.fromisoformat(scraper.get_update_after(route)) - base).total_seconds() / 3600
    assert low <= hours <= high


def test_update_after_retries_failed_routes_sooner(monkeypatch):
    base = freeze_now(monkeypatch)
    route = {"origin": "CPH", "destination": "PMI", "date": "2027-06-01"}
    hours = (datetime.fromisoformat(scraper.get_update_after(route, failed=True)) - base).total_seconds() / 3600
    assert 0.9 <= hours <= 1.1


# --- get_work ----------------------------------------------------------------

def test_get_work_returns_due_routes_oldest_first_and_capped(monkeypatch):
    freeze_now(monkeypatch)
    routes = [
        {"origin": "CPH", "destination": f"D{i}", "date": "2027-02-01",
         "update_after": f"2027-01-01T0{i}:00:00+00:00"}
        for i in range(7)
    ]
    routes.append({"origin": "CPH", "destination": "LATER", "date": "2027-02-01",
                   "update_after": "2027-01-02T00:00:00+00:00"})  # not due yet
    work = scraper.get_work(routes)
    assert [r["destination"] for r in work] == ["D0", "D1", "D2", "D3", "D4"]


def test_get_work_is_empty_when_nothing_is_due(monkeypatch):
    freeze_now(monkeypatch)
    routes = [{"origin": "CPH", "destination": "PMI", "date": "2027-02-01",
               "update_after": "2027-01-02T00:00:00+00:00"}]
    assert scraper.get_work(routes) == []


# --- extract -----------------------------------------------------------------

def fake_api(monkeypatch, offers):
    monkeypatch.setattr(scraper, "iter_offers", lambda origin, date_from=None: iter(offers))
    monkeypatch.setattr(scraper, "graphql", lambda query, variables: make_details())


def test_extract_keeps_only_the_route_and_the_cheapest_offer_per_flight(files, monkeypatch):
    freeze_now(monkeypatch)
    fake_api(monkeypatch, [
        make_offer(price=2000, key="expensive"),            # same flight, pricier package
        make_offer(price=1000, key="cheap"),
        make_offer(dest="AGP", price=500),                  # other destination
        make_offer(day="20270106", price=500),              # other day, offers are sorted so we stop here
    ])
    route = {"origin": "CPH", "destination": "PMI", "date": "2027-01-05",
             "last_updated": None, "update_after": "2027-01-01T00:00:00+00:00"}

    flights = scraper.extract(route)

    assert len(flights) == 1
    assert flights[0]["price"] == 100.0
    assert route["last_updated"] == "2027-01-01T12:00:00+00:00"
    assert route["update_after"] > route["last_updated"]


def test_extract_stops_paginating_once_past_the_route_date(files, monkeypatch):
    freeze_now(monkeypatch)

    def offers():
        yield make_offer()
        yield make_offer(day="20270106")
        raise AssertionError("kept fetching pages after the route date")

    monkeypatch.setattr(scraper, "iter_offers", lambda origin, date_from=None: offers())
    monkeypatch.setattr(scraper, "graphql", lambda query, variables: make_details())
    route = {"origin": "CPH", "destination": "PMI", "date": "2027-01-05",
             "last_updated": None, "update_after": "2027-01-01T00:00:00+00:00"}

    assert len(scraper.extract(route)) == 1


def test_extract_replaces_old_results_of_the_same_route_only(files, monkeypatch):
    freeze_now(monkeypatch)
    other = {"iata_origin": "CPH", "iata_destination": "AGP", "departure": "2027-01-05T08:00:00", "price": 1.0}
    stale = {"iata_origin": "CPH", "iata_destination": "PMI", "departure": "2027-01-05T06:10:00", "price": 999.0}
    (files / "results.json").write_text(json.dumps([other, stale]))
    fake_api(monkeypatch, [make_offer(price=1000)])
    route = {"origin": "CPH", "destination": "PMI", "date": "2027-01-05",
             "last_updated": None, "update_after": "2027-01-01T00:00:00+00:00"}

    scraper.extract(route)

    saved = json.loads((files / "results.json").read_text())
    assert other in saved
    assert stale not in saved
    assert [f["price"] for f in saved if f["iata_destination"] == "PMI"] == [100.0]


def test_extract_clears_a_route_that_sold_out(files, monkeypatch):
    freeze_now(monkeypatch)
    stale = {"iata_origin": "CPH", "iata_destination": "PMI", "departure": "2027-01-05T06:10:00", "price": 999.0}
    (files / "results.json").write_text(json.dumps([stale]))
    fake_api(monkeypatch, [])
    route = {"origin": "CPH", "destination": "PMI", "date": "2027-01-05",
             "last_updated": None, "update_after": "2027-01-01T00:00:00+00:00"}

    assert scraper.extract(route) == []
    assert json.loads((files / "results.json").read_text()) == []


# --- discover_routes ---------------------------------------------------------

def test_discover_routes_dedupes_and_keeps_existing_timestamps(files, monkeypatch):
    freeze_now(monkeypatch)
    monkeypatch.setattr(scraper, "ORIGINS", {"CPH": "1"})
    monkeypatch.setattr(scraper, "iter_offers", lambda origin, date_from=None: iter([
        make_offer(price=1), make_offer(price=2),           # same route twice (different return dates)
        make_offer(day="20270112"),
    ]))
    known = {"origin": "CPH", "destination": "PMI", "date": "2027-01-05",
             "last_updated": "2026-12-31T00:00:00+00:00", "update_after": "2027-01-10T00:00:00+00:00"}
    (files / "routes.json").write_text(json.dumps([known]))

    routes = scraper.discover_routes()

    assert len(routes) == 2
    assert known in routes
    new = next(r for r in routes if r["date"] == "2027-01-12")
    assert new["last_updated"] is None
    assert json.loads((files / "routes.json").read_text()) == routes
