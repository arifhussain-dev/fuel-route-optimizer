
import json
import math
import threading
import time
from pathlib import Path

import requests

# --- Tunables (also documented in README) ------------------------------------
TANK_RANGE_MILES = 500.0   # vehicle max range on a full tank
MPG = 10.0                # fuel efficiency
STATION_SEARCH_RADIUS_MILES = 25.0  # how far off-route a station may be
CELL_DEG = 0.5            # spatial grid cell size for station matching

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "spotter-fuel-assessment/1.0 (contact: arif.hussain.cswhiz@gmail.com)"

STATIONS_PATH = Path(__file__).resolve().parent / "data" / "stations.json"

_geocode_cache = {}
_stations = None


# --- geo helpers --------------------------------------------------------------
def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_stations():
    """Load pre-geocoded stations once; returns list of dicts."""
    global _stations
    if _stations is None:
        with open(STATIONS_PATH) as f:
            _stations = json.load(f)
    return _stations


_nominatim_lock = threading.Lock()
_nominatim_last_start = 0.0  # monotonic() time of the last request start
NOMINATIM_MIN_INTERVAL = 1.1  # seconds between Nominatim request starts


def _nominatim_get(params):
    """Single paced Nominatim GET; returns the requests Response."""
    global _nominatim_last_start
    with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last_start)
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(
                NOMINATIM_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        finally:
            _nominatim_last_start = time.monotonic()


# --- external APIs (<=3 calls per optimize request) ------------------------------
def geocode(place, _tally=None):
    """Nominatim geocode restricted to the USA, with in-memory cache.

    Real HTTP requests go through _nominatim_get: process-wide pacing,
    sequential, per the Nominatim usage policy (max 1 request/second).
    """
    key = place.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    resp = _nominatim_get(
        {"q": place, "format": "json", "limit": 1, "countrycodes": "us"})
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Could not geocode location: {place!r}")
    top = data[0]
    result = {
        "lat": float(top["lat"]),
        "lng": float(top["lon"]),
        "display_name": top["display_name"],
    }
    _geocode_cache[key] = result
    if _tally is not None:
        _tally["n"] += 1
    return result


def get_route(start, finish, _tally=None):
    """Single OSRM call. Returns GeoJSON geometry + distance/duration."""
    url = f"{OSRM_URL}/{start['lng']},{start['lat']};{finish['lng']},{finish['lat']}"
    resp = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("Routing failed for the given locations.")
    if _tally is not None:
        _tally["n"] += 1
    route = data["routes"][0]
    return {
        "geometry": route["geometry"],  # GeoJSON LineString
        "distance_miles": route["distance"] / 1609.344,
        "duration_minutes": route["duration"] / 60.0,
    }


# --- station matching ----------------------------------------------------------
def cumulative_miles(coords):
    """Cumulative road miles along a [[lng, lat], ...] polyline."""
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(
            cum[-1]
            + haversine_miles(coords[i - 1][1], coords[i - 1][0],
                              coords[i][1], coords[i][0])
        )
    return cum


def stations_near_route(coords, cum):
    """Stations within STATION_SEARCH_RADIUS_MILES of the route.

    Uses a spatial grid over route points so this stays fast
    (~thousands of stations x local cells only).
    Returns stations sorted by distance along the route (route_mile).
    """
    stations = load_stations()
    grid = {}
    for i, (lng, lat) in enumerate(coords):
        grid.setdefault((math.floor(lat / CELL_DEG),
                         math.floor(lng / CELL_DEG)), []).append(i)

    radius = STATION_SEARCH_RADIUS_MILES
    dlat = radius / 69.0
    n = max(1, math.ceil(dlat / CELL_DEG) + 1)

    found = {}
    for s in stations:
        clat = math.floor(s["lat"] / CELL_DEG)
        clng = math.floor(s["lng"] / CELL_DEG)
        best = None
        for a in range(clat - n, clat + n + 1):
            for b in range(clng - n, clng + n + 1):
                for i in grid.get((a, b), ()):
                    lng, lat = coords[i]
                    d = haversine_miles(s["lat"], s["lng"], lat, lng)
                    if d <= radius and (best is None or d < best[0]):
                        best = (d, cum[i])
        if best is not None:
            found[s["id"]] = {**s, "route_mile": best[1],
                              "off_route_miles": round(best[0], 1)}
    return sorted(found.values(), key=lambda x: x["route_mile"])


# --- fuel-stop optimization -----------------------------------------------------
def optimize_fuel_stops(candidates, total_miles):
    """Minimum-cost fueling plan (classic greedy, optimal for this model).

    Vehicle starts with a FULL tank (500-mile range). At each stop, buy just
    enough to reach the next cheaper station within range — otherwise fill up.
    The destination acts as a $0 station so we never over-buy.
    """
    points = sorted(candidates, key=lambda x: x["route_mile"])
    points = points + [{"route_mile": total_miles, "price": 0.0,
                        "sentinel": True}]

    # feasibility: no gap (start -> stations -> destination) may exceed range
    prev = 0.0
    for p in points:
        if p["route_mile"] - prev > TANK_RANGE_MILES + 1e-6:
            raise ValueError(
                "Route has a stretch longer than the 500-mile vehicle range "
                "with no fuel station nearby; cannot complete."
            )
        prev = p["route_mile"]

    fuel = TANK_RANGE_MILES  # miles of range remaining; starts full
    prev_mile = 0.0
    stops, total_cost, total_gallons = [], 0.0, 0.0

    for i, s in enumerate(points):
        fuel -= s["route_mile"] - prev_mile
        if s.get("sentinel"):
            break
        # next cheaper station within one tank of range (else: fill up)
        target_mile = min(s["route_mile"] + TANK_RANGE_MILES, total_miles)
        for t in points[i + 1:]:
            if t["route_mile"] - s["route_mile"] > TANK_RANGE_MILES:
                break
            if t["price"] < s["price"]:
                target_mile = t["route_mile"]
                break
        buy_miles = max(0.0, target_mile - s["route_mile"] - fuel)
        if buy_miles > 0:
            gallons = buy_miles / MPG
            cost = gallons * s["price"]
            total_cost += cost
            total_gallons += gallons
            stops.append({
                "name": s["name"],
                "city": s["city"],
                "state": s["state"],
                "lat": s["lat"],
                "lng": s["lng"],
                "price_per_gallon": round(s["price"], 3),
                "route_mile": round(s["route_mile"], 1),
                "off_route_miles": s["off_route_miles"],
                "gallons": round(gallons, 2),
                "cost_usd": round(cost, 2),
            })
            fuel += buy_miles
        prev_mile = s["route_mile"]

    return stops, round(total_cost, 2), round(total_gallons, 2)


# --- orchestration ---------------------------------------------------------------
def plan_route(start_query, finish_query):
    """Full pipeline: geocode x2 (sequential, per Nominatim policy)
    -> route x1 -> match -> optimize."""
    tally = {"n": 0}  # actual external HTTP calls (cache hits don't count)
    start = geocode(start_query, _tally=tally)
    finish = geocode(finish_query, _tally=tally)
    if haversine_miles(start["lat"], start["lng"],
                        finish["lat"], finish["lng"]) < 0.5:
        raise ValueError("Start and finish resolve to the same location.")
    route = get_route(start, finish, _tally=tally)
    coords = route["geometry"]["coordinates"]
    cum = cumulative_miles(coords)
   
    total_miles = route["distance_miles"]
    if cum and cum[-1] > 0:
        scale = total_miles / cum[-1]
        cum = [m * scale for m in cum]
    candidates = stations_near_route(coords, cum)
    stops, total_cost, total_gallons = optimize_fuel_stops(candidates,
                                                          total_miles)
    return {
        "start": {"query": start_query, **start},
        "finish": {"query": finish_query, **finish},
        "route": {
            "distance_miles": round(total_miles, 1),
            "duration_minutes": round(route["duration_minutes"], 1),
            "geometry": route["geometry"],
        },
        "fuel_stops": stops,
       
        "total_fuel_cost_usd": total_cost,
        "total_gallons_purchased": total_gallons,
        "total_gallons_consumed": round(total_miles / MPG, 2),
        "stations_considered": len(candidates),
        "assumptions": {
            "tank_range_miles": TANK_RANGE_MILES,
            "mpg": MPG,
            "starts_with_full_tank": True,
            "cost_basis": ("fuel purchased en route; the initial full tank "
                            "is a sunk cost outside the trip"),
            "station_search_radius_miles": STATION_SEARCH_RADIUS_MILES,
            "station_coordinates": "city-level centroids from provided CSV",
        },
        "meta": {"external_api_calls": tally["n"]},
    }
