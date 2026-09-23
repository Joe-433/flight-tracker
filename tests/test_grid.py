from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.sources.grid import Combo, GridScanner, combos, windows

TODAY = dt.date(2026, 9, 12)


class FakeScanner(GridScanner):
    """GridScanner with a scripted network."""

    def __init__(self, cfg, responses):
        super().__init__(cfg)
        self.responses = list(responses)

    def _fetch(self, combo, first, last):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestWindows(unittest.TestCase):
    def test_splits_at_61_days(self):
        spans = windows(TODAY, TODAY + dt.timedelta(days=91))
        self.assertEqual(len(spans), 2)
        self.assertEqual((spans[0][1] - spans[0][0]).days, 60)
        self.assertEqual(spans[1][1], TODAY + dt.timedelta(days=91))

    def test_short_range_is_one_window(self):
        self.assertEqual(len(windows(TODAY, TODAY + dt.timedelta(days=10))), 1)


class TestCombos(unittest.TestCase):
    def test_every_destination_length_and_stop_class(self):
        cfg = make_config(search={"trip_nights": [5, 6, 7], "max_stops": 1})
        cfg.route.destinations = ["LAX", "BUR", "SNA", "ONT", "LGB"]
        self.assertEqual(len(combos(cfg)), 30)

    def test_nonstop_only_halves_it(self):
        cfg = make_config(search={"trip_nights": [5, 6, 7], "max_stops": 0})
        cfg.route.destinations = ["LAX", "BUR", "SNA", "ONT", "LGB"]
        self.assertEqual(len(combos(cfg)), 15)

    def test_key_round_trips(self):
        combo = Combo("ONT", 6, False)
        self.assertEqual(combo.key, "ONT|6|1")
        self.assertEqual(Combo.from_key(combo.key), combo)


class TestScan(unittest.TestCase):
    def cfg(self):
        return make_config(search={"min_days_ahead": 14, "window_days": 9})

    def day(self, offset):
        return (TODAY + dt.timedelta(days=offset)).isoformat()

    def test_keeps_only_the_asked_trip_length_and_horizon(self):
        cfg = self.cfg()
        rows = [
            (self.day(15), self.day(20), 300.0),  # in range, 5 nights: keep
            (self.day(16), self.day(22), 250.0),  # 6 nights: drop
            (self.day(10), self.day(15), 200.0),  # before the horizon: drop
            (self.day(40), self.day(45), 150.0),  # after the horizon: drop
        ]
        prices = FakeScanner(cfg, [rows]).scan(Combo("LAX", 5, True), today=TODAY)
        self.assertEqual(prices, {self.day(15): 300.0})

    def test_duplicate_dates_keep_the_cheapest(self):
        rows = [(self.day(15), self.day(20), 300.0), (self.day(15), self.day(20), 280.0)]
        prices = FakeScanner(self.cfg(), [rows]).scan(Combo("LAX", 5, True), today=TODAY)
        self.assertEqual(prices[self.day(15)], 280.0)

    def test_total_failure_is_none(self):
        scanner = FakeScanner(self.cfg(), [RuntimeError("blocked")])
        self.assertIsNone(scanner.scan(Combo("LAX", 5, True), today=TODAY))
        self.assertIn("blocked", scanner.errors[0])

    def test_partial_failure_keeps_what_worked(self):
        cfg = make_config(search={"min_days_ahead": 0, "window_days": 90})
        good = [(self.day(5), self.day(10), 300.0)]
        scanner = FakeScanner(cfg, [good, RuntimeError("second window broke")])
        prices = scanner.scan(Combo("LAX", 5, True), today=TODAY)
        self.assertEqual(prices, {self.day(5): 300.0})
        self.assertEqual(len(scanner.errors), 1)

    def test_empty_answer_is_not_a_failure(self):
        """No nonstops into Long Beach is a fact, not an outage."""
        prices = FakeScanner(self.cfg(), [[]]).scan(Combo("LGB", 5, True), today=TODAY)
        self.assertEqual(prices, {})

    def test_counts_requests(self):
        cfg = make_config(search={"min_days_ahead": 0, "window_days": 90})
        scanner = FakeScanner(cfg, [[], []])
        scanner.scan(Combo("LAX", 5, True), today=TODAY)
        self.assertEqual(scanner.requests, 2)


class TestOrigins(unittest.TestCase):
    def test_excluded_airports_are_left_out(self):
        scanner = FakeScanner(make_config(), [])
        self.assertEqual(scanner.origins(), ["JFK", "LGA"])


if __name__ == "__main__":
    unittest.main()
