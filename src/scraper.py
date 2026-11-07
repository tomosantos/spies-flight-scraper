"""Scraper for one-way charter flights sold on spies.dk.

The site's flight search talks to a public GraphQL API (origo-sc.nltg.com),
so everything here is plain `requests`, no browser needed.
"""
import argparse
import json
import logging
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from functools import cache

import requests

API_URL = "https://origo-sc.nltg.com/"
HEADERS = {
    "content-type": "application/json",
    "origin": "https://www.spies.dk",
    "referer": "https://www.spies.dk/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "auth-userid": "2645",
    "marketunit": "sd",
    "x-caller-app": "flight-only-charter",
    "x-origo-context": "production",
}

RESULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "result")
ROUTES_FILE = os.path.join(RESULT_DIR, "routes.json")
RESULTS_FILE = os.path.join(RESULT_DIR, "results.json")

# Departure airports offered by the site's search form (IATA -> internal id)
ORIGINS = {
    "CPH": "12678", "AAL": "12675", "AAR": "13303", "BLL": "12676", "RNN": "12677",
    "HAM": "13305", "KRP": "13304", "ODE": "37057", "SGD": "19114",
}

# The API gives some carriers as ICAO codes, the output wants IATA (two letters).
# Airseven (CAT) has no IATA code at all, so it ends up as None.
ICAO_TO_IATA = {"JTD": "JP"}

PAGE_SIZE = 50
DELAY = 2.0  # seconds between API calls, the API answers 429 to fast clients
FALLBACK_DKK_EUR = 1 / 7.46  # the krone is pegged to the euro

log = logging.getLogger("spies")

OFFER_FIELDS = """
    meta { departureCode destinationCode }
    includedTransportOffer { rawTotalPrice transportDetailsKey }
    out { carrier { key } departure { date { url time } } }
"""

OFFERS_QUERY = """
query Offers($dep: [ID!], $ages: [Int], $n: Int, $from: String) {
  team1FlightOnlyFlightOffers(departureCaId: $dep, ages: $ages, nrOfFlightOffers: $n,
                              departureDateFrom: $from, tripType: FlightOnly) {
    flightOffers { %s }
    keys { next }
  }
}""" % OFFER_FIELDS

MORE_QUERY = """
query More($key: ID, $n: Int) {
  team1FlightOnlyFlightOffers(key: $key, nrOfFlightOffers: $n) {
    flightOffers { %s }
    keys { next }
  }
}""" % OFFER_FIELDS

DETAILS_QUERY = """
query Details($key: String!) {
  q2Transport(key: $key) { transport { out { flightDetails {
    ... on BookinghubFlightDetailsWithLegsV2 { legs {
      __typename
      ... on BookinghubFlightLegV2 {
        legDetails { duration { raw } }
        destination { date { time } }
      }
    } }
  } } } }
}"""

session = requests.Session()
session.headers.update(HEADERS)


# --- helpers -----------------------------------------------------------------

def now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@cache
def dkk_to_eur():
    try:
        r = requests.get("https://api.frankfurter.dev/v1/latest",
                         params={"base": "DKK", "symbols": "EUR"}, timeout=15)
        r.raise_for_status()
        return r.json()["rates"]["EUR"]
    except (requests.RequestException, KeyError, ValueError):
        log.warning("could not fetch DKK/EUR rate, using fallback")
        return FALLBACK_DKK_EUR


def graphql(query, variables, retries=5):
    """POST to the API, backing off on rate limits and server errors."""
    for attempt in range(retries):
        wait = 30 * 2 ** attempt
        try:
            r = session.post(API_URL, json={"query": query, "variables": variables}, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = max(wait, int(r.headers.get("retry-after", 0)))
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(body["errors"][0]["message"])
            time.sleep(DELAY)
            return body["data"]
        except requests.RequestException as exc:
            log.warning("request failed (%s), retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError("API unavailable after retries")


def iter_offers(origin, date_from=None):
    """Yield every offer for an origin airport, following the pagination key."""
    data = graphql(OFFERS_QUERY, {"dep": [ORIGINS[origin]], "ages": [42],
                                  "n": PAGE_SIZE, "from": date_from})
    while True:
        page = data["team1FlightOnlyFlightOffers"]
        yield from page["flightOffers"]
        next_key = page["keys"]["next"]
        if not next_key:
            return
        data = graphql(MORE_QUERY, {"key": next_key, "n": PAGE_SIZE})


# --- routes ------------------------------------------------------------------

def discover_routes():
    """Walk all offers from every origin and collect origin/destination/date."""
    found = set()
    for origin in ORIGINS:
        before = len(found)
        for offer in iter_offers(origin):
            day = datetime.strptime(offer["out"]["departure"]["date"]["url"], "%Y%m%d").date()
            found.add((origin, offer["meta"]["destinationCode"], day.isoformat()))
        log.info("%s: %d routes", origin, len(found) - before)

    old = {(r["origin"], r["destination"], r["date"]): r for r in load_json(ROUTES_FILE, [])}
    routes = []
    for origin, destination, day in sorted(found):
        route = old.get((origin, destination, day)) or {
            "origin": origin,
            "destination": destination,
            "date": day,
            "last_updated": None,
            "update_after": now().isoformat(timespec="seconds"),
        }
        routes.append(route)
    # every new route is due at the same time, shuffle so the first batches
    # cover many destinations instead of one origin's dates in order
    random.shuffle(routes)
    save_json(ROUTES_FILE, routes)
    return routes


def get_work(routes, limit=5):
    """The `limit` routes that are due, oldest update_after first."""
    due = [r for r in routes if r["update_after"] <= now().isoformat(timespec="seconds")]
    return sorted(due, key=lambda r: r["update_after"])[:limit]


def get_update_after(route, failed=False):
    """Next time a route should be refreshed: soon for close dates, rarely for far ones."""
    if failed:
        hours = 1
    else:
        days_left = (date.fromisoformat(route["date"]) - now().date()).days
        hours = 6 if days_left <= 7 else 12 if days_left <= 30 else 24
    delay = timedelta(hours=hours) * random.uniform(0.9, 1.1)  # spread the load
    return (now() + delay).isoformat(timespec="seconds")


# --- flights -----------------------------------------------------------------

def carrier_code(key):
    """'flight|DK' -> 'DK'. Falls back to the ICAO->IATA table for 3-letter codes."""
    code = key.split("|")[-1]
    return code if len(code) == 2 else ICAO_TO_IATA.get(code)


def build_flight(offer, details):
    """Map an offer + its detail legs to the flight format. None if not a direct flight."""
    flight_details = details["q2Transport"]["transport"]["out"]["flightDetails"]
    legs = flight_details and flight_details.get("legs")
    if not legs or len(legs) != 1 or legs[0]["__typename"] != "BookinghubFlightLegV2":
        return None
    leg = legs[0]

    out = offer["out"]["departure"]["date"]
    departure = datetime.strptime(out["url"] + out["time"], "%Y%m%d%H:%M")
    h, m, s = map(int, leg["legDetails"]["duration"]["raw"].split(":"))
    duration = timedelta(hours=h, minutes=m, seconds=s)

    # The API gives the arrival clock time in the destination's timezone, so pick
    # the calendar day that lands closest to departure + duration.
    arr_time = datetime.strptime(leg["destination"]["date"]["time"], "%H:%M").time()
    expected = departure + duration
    arrival = min(
        (datetime.combine(expected.date() + timedelta(days=d), arr_time) for d in (-1, 0, 1)),
        key=lambda c: abs(c - expected),
    )

    return {
        "departure": departure.isoformat(),
        "arrival": arrival.isoformat(),
        "duration": round(duration.total_seconds() / 3600, 2),
        "carrier": carrier_code(offer["out"]["carrier"]["key"]),
        "iata_origin": offer["meta"]["departureCode"],
        "iata_destination": offer["meta"]["destinationCode"],
        "price": round(offer["includedTransportOffer"]["rawTotalPrice"] * dkk_to_eur(), 2),
        "stops": 0,
        "layover_info": None,
        "flight_numbers": None,  # not exposed by the API
        "vehicle_type": "aircraft",
    }


def extract(route):
    """Fetch the route's flights from the site and update results.json."""
    day = route["date"].replace("-", "")

    # One flight can show up in several offers (different return dates); keep the cheapest.
    cheapest = {}
    for offer in iter_offers(route["origin"], date_from=route["date"]):
        offer_day = offer["out"]["departure"]["date"]["url"]
        if offer_day > day:  # offers come sorted by date
            break
        if offer_day != day or offer["meta"]["destinationCode"] != route["destination"]:
            continue
        key = offer["out"]["departure"]["date"]["time"]
        price = offer["includedTransportOffer"]["rawTotalPrice"]
        if key not in cheapest or price < cheapest[key]["includedTransportOffer"]["rawTotalPrice"]:
            cheapest[key] = offer

    flights = []
    for offer in cheapest.values():
        details = graphql(DETAILS_QUERY, {"key": offer["includedTransportOffer"]["transportDetailsKey"]})
        flight = build_flight(offer, details)
        if flight:
            flights.append(flight)

    def same_route(f):
        return (f["iata_origin"], f["iata_destination"], f["departure"][:10]) == (
            route["origin"], route["destination"], route["date"])

    results = [f for f in load_json(RESULTS_FILE, []) if not same_route(f)]
    save_json(RESULTS_FILE, results + flights)

    route["last_updated"] = now().isoformat(timespec="seconds")
    route["update_after"] = get_update_after(route)
    return flights


def main():
    parser = argparse.ArgumentParser(description="spies.dk flight scraper")
    parser.add_argument("--discover", action="store_true", help="rebuild routes.json first")
    parser.add_argument("--max-routes", type=int, help="stop after scraping this many routes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    routes = load_json(ROUTES_FILE, [])
    if args.discover or not routes:
        routes = discover_routes()
        log.info("discovered %d routes", len(routes))

    done = 0
    while args.max_routes is None or done < args.max_routes:
        work = get_work(routes)
        if args.max_routes is not None:
            work = work[: args.max_routes - done]
        if not work:
            if args.max_routes is not None:
                break
            time.sleep(60)  # nothing due yet, check again later
            continue
        for route in work:
            try:
                flights = extract(route)
                log.info("%s-%s %s: %d flights", route["origin"], route["destination"],
                         route["date"], len(flights))
            except Exception:
                log.exception("failed %s-%s %s", route["origin"], route["destination"], route["date"])
                route["update_after"] = get_update_after(route, failed=True)
            save_json(ROUTES_FILE, routes)
            done += 1


if __name__ == "__main__":
    main()
