from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.sources.base import date_pairs
from flight_tracker.sources.mock import MockSource
from flight_tracker.sources.pairs import _format_flight_no, carrier_matches

TODAY = dt.date(2026, 9, 12)


class TestDatePairs(unittest.TestCase):
    def test_window_bounds_departures_not_returns(self):
        """A return leg may land past the window end.

        Clamping it would silently drop every trip departing in the final week.
        """
        cfg = make_config(
            search={"window_days": 14, "min_days_ahead": 0, "trip_nights": [7]}
        )
        pairs = date_pairs(cfg, today=TODAY)
        last_out, last_ret = pairs[-1]
        self.assertEqual(last_out, (TODAY + dt.timedelta(days=14)).isoformat())
        self.assertEqual(last_ret, (TODAY + dt.timedelta(days=21)).isoformat())

    def test_count_is_window_times_lengths(self):
        cfg = make_config(search={"window_days": 83, "trip_nights": [3, 4, 5, 6, 7]})
        self.assertEqual(len(date_pairs(cfg, today=TODAY)), 84 * 5)

    def test_min_days_ahead_shifts_window(self):
        cfg = make_config(search={"min_days_ahead": 14, "trip_nights": [3]})
        first = date_pairs(cfg, today=TODAY)[0][0]
        self.assertEqual(first, (TODAY + dt.timedelta(days=14)).isoformat())

    def test_nights_respected(self):
        cfg = make_config(search={"trip_nights": [4]})
        for out_date, ret_date in date_pairs(cfg, today=TODAY):
            delta = dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)
            self.assertEqual(delta.days, 4)


class TestMockSource(unittest.TestCase):
    ITEMS = [
        ("2026-10-01", "2026-10-06", "LAX"),
        ("2026-10-02", "2026-10-07", "ONT"),
    ]

    def test_deterministic(self):
        cfg = make_config()
        first = MockSource(cfg).drill(self.ITEMS)
        second = MockSource(cfg).drill(self.ITEMS)
        self.assertEqual([o.price for o in first], [o.price for o in second])

    def test_emits_both_stop_classes_when_connections_allowed(self):
        cfg = make_config(search={"max_stops": 1})
        offers = MockSource(cfg).drill(self.ITEMS)
        self.assertEqual(len(offers), 4)
        self.assertEqual(sorted({o.stops for o in offers}), [0, 1])

    def test_nonstop_only_when_configured(self):
        cfg = make_config(search={"max_stops": 0})
        offers = MockSource(cfg).drill(self.ITEMS)
        self.assertEqual(len(offers), 2)
        self.assertTrue(all(o.stops == 0 for o in offers))

    def test_destination_lands_in_the_history_key(self):
        """Fares into different airports must never share a price history."""
        offers = MockSource(make_config()).drill(self.ITEMS)
        self.assertEqual({o.arr_airport for o in offers}, {"LAX", "ONT"})
        self.assertEqual(len({o.key for o in offers}), len(offers))

    def test_drills_exactly_what_it_is_given(self):
        offers = MockSource(make_config(search={"max_stops": 0})).drill(self.ITEMS[:1])
        self.assertEqual(
            [(o.out_date, o.ret_date, o.arr_airport) for o in offers], self.ITEMS[:1]
        )


class TestFlightNumberFormatting(unittest.TestCase):
    def test_nonstop(self):
        self.assertEqual(_format_flight_no(["AA 171"], 1), "AA 171")

    def test_connection_shows_both_numbers(self):
        self.assertEqual(_format_flight_no(["AA 171", "AA 2345"], 2), "AA 171 / 2345")

    def test_carrier_repeated_only_when_it_changes(self):
        self.assertEqual(
            _format_flight_no(["WN 2536", "UA 44"], 2), "WN 2536 / UA 44"
        )

    def test_three_segments(self):
        self.assertEqual(
            _format_flight_no(["B6 1", "B6 2", "B6 3"], 3), "B6 1 / 2 / 3"
        )

    def test_partial_data_is_dropped(self):
        self.assertIsNone(_format_flight_no(["AA 171", None], 2))
        self.assertIsNone(_format_flight_no([], 1))

    def test_misalignment_is_dropped_rather_than_guessed(self):
        """Showing the wrong flight number is worse than showing none."""
        self.assertIsNone(_format_flight_no(["AA 171"], 2))


class TestCarrierMatch(unittest.TestCase):
    """Guards against the two payload walks drifting out of alignment."""

    def test_code_inside_the_name(self):
        self.assertTrue(carrier_matches("AS 289", ["Alaska"]))

    def test_code_unrelated_to_the_name(self):
        self.assertTrue(carrier_matches("WN 2536", ["Southwest"]))
        self.assertTrue(carrier_matches("B6 523", ["JetBlue"]))
        self.assertTrue(carrier_matches("F9 4601", ["Frontier"]))

    def test_mismatch_is_caught(self):
        self.assertFalse(carrier_matches("WN 2536", ["American"]))
        self.assertFalse(carrier_matches("AA 117", ["Delta"]))

    def test_codeshare_lists_pass_if_any_airline_matches(self):
        self.assertTrue(carrier_matches("AA 117", ["American", "Alaska"]))

    def test_nothing_to_check_passes(self):
        self.assertTrue(carrier_matches(None, ["Delta"]))
        self.assertTrue(carrier_matches("DL 1", []))


if __name__ == "__main__":
    unittest.main()
