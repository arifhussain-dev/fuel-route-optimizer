
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, Client

from fuelapi import services
from fuelapi.services import (
    TANK_RANGE_MILES, MPG, haversine_miles, optimize_fuel_stops,
)


def station(mile, price, name="S"):
    return {"id": f"{mile}", "name": name, "city": "C", "state": "ST",
            "lat": 0.0, "lng": 0.0, "price": price, "route_mile": mile,
            "off_route_miles": 1.0}


class HaversineTests(SimpleTestCase):
    def test_nyc_to_chicago_straight_line(self):
        d = haversine_miles(40.7128, -74.0060, 41.8781, -87.6298)
        self.assertTrue(700 < d < 725, f"unexpected: {d}")

    def test_zero_distance(self):
        self.assertAlmostEqual(haversine_miles(40, -74, 40, -74), 0.0)


class OptimizeFuelStopsTests(SimpleTestCase):
    def test_basic_decreasing_prices(self):
        # 700-mi trip; hand-computed: buy 20 gal @ $3.00 at mile 300 only.
        stops, cost, gallons = optimize_fuel_stops(
            [station(100, 4.00), station(300, 3.00), station(600, 3.50)], 700)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["route_mile"], 300)
        self.assertAlmostEqual(stops[0]["gallons"], 20.0)
        self.assertAlmostEqual(cost, 60.0)
        self.assertAlmostEqual(gallons, 20.0)
        # reconciliation: consumed = 50 (initial tank) + purchased
        self.assertAlmostEqual(gallons, 700 / MPG - TANK_RANGE_MILES / MPG)

    def test_station_exactly_at_range_boundary(self):
        # station exactly 500 miles out must be reachable, not a gap
        stops, cost, gallons = optimize_fuel_stops(
            [station(500, 3.00), station(800, 4.00)], 1000)
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(gallons, 50.0)
        self.assertAlmostEqual(cost, 150.0)

    def test_gap_bigger_than_range_is_infeasible(self):
        with self.assertRaises(ValueError):
            optimize_fuel_stops([station(100, 3.00), station(700, 3.00)], 1200)

    def test_no_stations_short_route(self):
        stops, cost, gallons = optimize_fuel_stops([], 400)
        self.assertEqual((stops, cost, gallons), ([], 0.0, 0.0))

    def test_no_stations_long_route_infeasible(self):
        with self.assertRaises(ValueError):
            optimize_fuel_stops([], 600)

    def test_buys_just_enough_to_reach_cheaper_station(self):
        # must stop at expensive s1 (can't reach s2 on initial tank),
        # but buys only what is needed to get to cheaper s2
        stops, cost, gallons = optimize_fuel_stops(
            [station(300, 4.00), station(600, 3.00)], 800)
        self.assertEqual(len(stops), 2)
        self.assertAlmostEqual(stops[0]["gallons"], 10.0)  # 100 miles worth
        self.assertAlmostEqual(stops[1]["gallons"], 20.0)
        self.assertAlmostEqual(cost, 10 * 4.00 + 20 * 3.00)

    def test_cost_is_sum_of_gallons_times_price(self):
        stops, cost, _ = optimize_fuel_stops(
            [station(100, 4.00), station(300, 3.00), station(600, 3.50)], 700)
        self.assertAlmostEqual(
            cost, sum(s["gallons"] * s["price_per_gallon"] for s in stops))

    def test_never_buys_more_than_needed_for_destination(self):
        # destination acts as a $0 station: no over-buying past it
        stops, cost, gallons = optimize_fuel_stops([station(100, 5.00)], 600)
        # 600/10 - 50 = 10 gal needed; all bought at the single station
        self.assertAlmostEqual(gallons, 10.0)
        self.assertAlmostEqual(cost, 50.0)


class PlanRouteTests(SimpleTestCase):
    @patch("fuelapi.services.get_route")
    @patch("fuelapi.services.geocode")
    def test_same_start_and_finish_rejected(self, mock_geocode, mock_route):
        mock_geocode.side_effect = [
            {"lat": 41.0, "lng": -87.0, "display_name": "Chicago"},
            {"lat": 41.0, "lng": -87.0, "display_name": "Chicago"},
        ]
        with self.assertRaises(ValueError):
            services.plan_route("Chicago, IL", "Chicago, IL")
        mock_route.assert_not_called()


class OptimizeViewTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_wrong_method(self):
        self.assertEqual(self.client.get("/api/optimize/").status_code, 405)

    def test_invalid_json(self):
        r = self.client.post("/api/optimize/", data="{bad",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_non_object_json(self):
        r = self.client.post("/api/optimize/", data="[1,2]",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_missing_fields(self):
        r = self.client.post("/api/optimize/", data=json.dumps({"start": "X"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_non_string_field(self):
        r = self.client.post("/api/optimize/",
                             data=json.dumps({"start": 123, "finish": "Y"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    @patch("fuelapi.views.services.plan_route")
    def test_success(self, mock_plan):
        mock_plan.return_value = {"total_fuel_cost_usd": 10.0}
        r = self.client.post("/api/optimize/",
                             data=json.dumps({"start": "A", "finish": "B"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_fuel_cost_usd"], 10.0)

    @patch("fuelapi.views.services.plan_route")
    def test_service_value_error_becomes_422(self, mock_plan):
        mock_plan.side_effect = ValueError("nope")
        r = self.client.post("/api/optimize/",
                             data=json.dumps({"start": "A", "finish": "B"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 422)
        self.assertIn("nope", r.json()["error"])

    @patch("fuelapi.views.services.plan_route")
    def test_upstream_failure_becomes_502(self, mock_plan):
        import requests
        mock_plan.side_effect = requests.ConnectionError("down")
        r = self.client.post("/api/optimize/",
                             data=json.dumps({"start": "A", "finish": "B"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 502)

    @patch("fuelapi.views.services.plan_route")
    def test_unexpected_error_becomes_500_without_leak(self, mock_plan):
        mock_plan.side_effect = RuntimeError("secret traceback detail")
        r = self.client.post("/api/optimize/",
                             data=json.dumps({"start": "A", "finish": "B"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret", r.json()["error"])


class GeocodeCacheTests(SimpleTestCase):
    def test_cache_avoids_repeat_call_and_tally(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{
            "lat": "40.7128", "lon": "-74.0060",
            "display_name": "New York, NY, USA"}]
        saved = dict(services._geocode_cache)
        services._geocode_cache.clear()
        try:
            with patch("fuelapi.services._nominatim_get",
                       return_value=mock_resp) as mock_paced:
                tally = {"n": 0}
                r1 = services.geocode("New York, NY", _tally=tally)
                r2 = services.geocode("New York, NY", _tally=tally)
            self.assertEqual(mock_paced.call_count, 1)
            self.assertEqual(tally["n"], 1)
            self.assertEqual(r1, r2)
            self.assertAlmostEqual(r1["lat"], 40.7128)
        finally:
            services._geocode_cache.clear()
            services._geocode_cache.update(saved)


class MockedEndToEndTest(SimpleTestCase):
    """Full plan_route() pipeline: mocked externals, real matching+optimizer.

    No real network calls. A synthetic NYC->Chicago straight-line route
    (authoritative distance 1200 mi) with six stations placed on it.
    """

    def _straight_route(self, n=200, miles=1200.0):
        lat1, lng1 = 40.7128, -74.0060   # NYC
        lat2, lng2 = 41.8781, -87.6298   # Chicago
        coords = [[lng1 + (lng2 - lng1) * i / (n - 1),
                   lat1 + (lat2 - lat1) * i / (n - 1)] for i in range(n)]
        return {
            "geometry": {"type": "LineString", "coordinates": coords},
            "distance_miles": miles,       # authoritative routing-engine value
            "duration_minutes": miles,     # 60 mph cruise
        }

    def _stations_on_route(self, coords, specs):
        n = len(coords)
        stations = []
        for idx, (frac, price, name) in enumerate(specs):
            i = round(frac * (n - 1))
            lng, lat = coords[i]
            stations.append({
                "id": f"T{idx}", "name": name, "address": f"{idx} Test Rd",
                "city": "Testville", "state": "TS", "price": price,
                "lat": lat, "lng": lng,
            })
        return stations

    def test_mocked_nyc_to_chicago_pipeline(self):
        route = self._straight_route()
        coords = route["geometry"]["coordinates"]
        specs = [(0.08, 4.00, "S1"), (0.25, 3.50, "S2"), (0.42, 3.80, "S3"),
                 (0.58, 3.20, "S4"), (0.75, 3.90, "S5"), (0.92, 3.60, "S6")]
        fake_stations = self._stations_on_route(coords, specs)

        def fake_geocode(place, _tally=None):
            if _tally is not None:
                _tally["n"] += 1
            if "chicago" in place.lower():
                return {"lat": 41.8781, "lng": -87.6298,
                        "display_name": place}
            return {"lat": 40.7128, "lng": -74.0060, "display_name": place}

        def fake_get_route(start, finish, _tally=None):
            if _tally is not None:
                _tally["n"] += 1
            return route

        with patch("fuelapi.services.load_stations",
                   return_value=fake_stations), \
             patch("fuelapi.services.geocode", side_effect=fake_geocode), \
             patch("fuelapi.services.get_route", side_effect=fake_get_route):
            result = services.plan_route("New York, NY", "Chicago, IL")

        # authoritative route distance drives the response
        self.assertAlmostEqual(result["route"]["distance_miles"], 1200.0)
        stops = result["fuel_stops"]
        self.assertGreaterEqual(len(stops), 2)  # optimizer did real work

        # stops ordered along the route
        miles = [s["route_mile"] for s in stops]
        self.assertEqual(miles, sorted(miles))

        # every consecutive fuel gap within vehicle range
        pts = [0.0] + miles + [result["route"]["distance_miles"]]
        self.assertTrue(
            all(b - a <= TANK_RANGE_MILES + 1e-6
                for a, b in zip(pts, pts[1:])))

        # gallons arithmetic
        self.assertAlmostEqual(result["total_gallons_purchased"],
                               sum(s["gallons"] for s in stops), places=2)
        self.assertAlmostEqual(result["total_gallons_consumed"],
                               1200.0 / MPG, places=2)
        # full-tank start: purchased == consumed - 50
        self.assertAlmostEqual(result["total_gallons_purchased"],
                               1200.0 / MPG - TANK_RANGE_MILES / MPG, places=1)

        # cost == sum(gallons * price)
        self.assertAlmostEqual(
            result["total_fuel_cost_usd"],
            sum(s["gallons"] * s["price_per_gallon"] for s in stops),
            places=1)

        # honest external-call tally: 2 geocodes + 1 route
        self.assertEqual(result["meta"]["external_api_calls"], 3)
