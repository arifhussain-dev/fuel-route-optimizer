# Fuel Route Optimizer

Django API that takes US start/finish locations and returns the driving route
plus cost-optimal fuel stops for a truck with a 500-mile range at 10 MPG.
Built for Spotter's Backend Engineer coding assessment.

## Pipeline

`POST /api/optimize/` → `plan_route()` in `fuelapi/services.py`:

1. **Geocode** start + finish (Nominatim, sequential, USA-restricted)
2. **Route** via OSRM — its distance is authoritative for all fuel math
3. **Match** stations within a 25-mile corridor using an in-memory spatial grid
4. **Optimize** fuel purchases with a greedy minimum-cost pass

At most **3 external HTTP calls** per request (2 geocoding + 1 routing).
Cached geocodes skip their calls, so the actual count ranges from 1 to 3 —
reported per request in `meta.external_api_calls`.

## Optimization

The vehicle starts with a **full tank** (50 gal). Stations are ordered by
route mile; the destination is treated as a $0 station so we never over-buy.
At each stop, buy just enough to reach the next cheaper station within
range — otherwise fill the tank. A feasibility pass rejects routes with any
gap over 500 miles between refueling opportunities.

This greedy is optimal under the model (point stations, fractional fuel,
known prices): any optimal plan can be transformed station-by-station to
match greedy without increasing cost.

## Cost basis

`total_fuel_cost_usd` covers fuel **purchased en route only** — the initial
full tank is a sunk cost. The response also returns `total_gallons_consumed`
(= miles / 10), so the numbers reconcile as
`purchased = max(0, consumed − 50)`.

## API

`POST /api/optimize/` with `{"start": "New York, NY", "finish": "Chicago, IL"}`

| Status | Meaning |
| ------ | ------- |
| 200 | Route, stops, costs |
| 400 | Invalid JSON, or missing / non-string fields |
| 405 | Non-POST method |
| 422 | Un-geocodable place, same start & finish, or infeasible route (>500-mi gap) |
| 502 | Upstream geocoding/routing failure |
| 500 | Unexpected error (no tracebacks leaked) |

Example (trimmed) — New York, NY → Chicago, IL:

```json
{
  "route": { "distance_miles": 790.6, "duration_minutes": 890.8, "geometry": {...} },
  "fuel_stops": [
    { "name": "SHEETZ #639", "city": "Youngstown", "state": "OH",
      "price_per_gallon": 3.059, "route_mile": 391.0,
      "gallons": 5.51, "cost_usd": 16.86 },
    { "name": "S&G #88", "city": "Toledo", "state": "OH",
      "price_per_gallon": 3.009, "route_mile": 555.1,
      "gallons": 23.55, "cost_usd": 70.85 }
  ],
  "total_fuel_cost_usd": 87.71,
  "total_gallons_purchased": 29.06,
  "total_gallons_consumed": 79.06,
  "stations_considered": 369,
  "meta": { "external_api_calls": 1 }
}
```

## Data

`fuelapi/data/stations.json` (6,626 US stations) is built from the provided
CSV (8,151 rows):

- 1,413 duplicate observations grouped by OPIS Truckstop ID → mean price kept
- 112 Canadian stations removed (USA-only scope)
- City → coordinates resolved at build time; **zero** valid US stations dropped

Reproduce: `python fuelapi/data/build_stations.py` (original CSV alongside).

Note: the CSV has no lat/lng, so stations sit on city-level centroids — the
25-mile matching corridor absorbs that; exact coordinates would cost ~6,600
extra geocode calls, far outside the 1–3 call budget.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py test fuelapi   # 22 tests
python manage.py runserver
```

- Demo map: http://127.0.0.1:8000/demo/
- Postman collection: `postman_collection.json`
- `SECRET_KEY` reads from `DJANGO_SECRET_KEY` (dev fallback for local runs); no secrets committed.
