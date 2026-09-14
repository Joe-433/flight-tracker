from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.config import Band
from flight_tracker.sources.base import date_pairs, plan_slice, slice_for_run
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


class TestSliceForRun(unittest.TestCase):
    def test_wraps_and_covers_everything(self):
        pairs = [("d%d" % i, "r%d" % i) for i in range(10)]
        seen = set()
        cursor = 0
        for _ in range(4):
            picked, cursor = slice_for_run(pairs, cursor, 3)
            seen.update(picked)
        self.assertEqual(len(seen), 10)

    def test_budget_larger_than_space(self):
        pairs = [("a", "b"), ("c", "d")]
        picked, cursor = slice_for_run(pairs, 0, 99)
        self.assertEqual(len(picked), 2)
        self.assertEqual(cursor, 0)

    def test_empty(self):
        self.assertEqual(slice_for_run([], 5, 3), ([], 0))


class TestPlanSlice(unittest.TestCase):
    def bands_config(self):
        return make_config(
            search={"min_days_ahead": 7, "window_days": 83, "trip_nights": [3, 4, 5, 6, 7]},
            source={
                "pairs_per_run": 20,
                "bands": [
                    Band(within_days=30, share=0.55),
                    Band(within_days=60, share=0.25),
                    Band(within_days=90, share=0.20),
                ],
            },
        )

    def test_budget_split_across_bands(self):
        picked, cursors = plan_slice(self.bands_config(), {}, today=TODAY)
        self.assertEqual(len(picked), 20)
        self.assertEqual(sorted(cursors), ["0", "1", "2"])
        leads = [(dt.date.fromisoformat(o) - TODAY).days for o, _ in picked]
        self.assertEqual(sum(1 for d in leads if d <= 30), 11)
        self.assertEqual(sum(1 for d in leads if 30 < d <= 60), 5)
        self.assertEqual(sum(1 for d in leads if d > 60), 4)

    def test_never_returns_a_pair_inside_min_days_ahead(self):
        cfg = self.bands_config()
        picked, _ = plan_slice(cfg, {}, today=TODAY)
        for out_date, _ in picked:
            self.assertGreaterEqual((dt.date.fromisoformat(out_date) - TODAY).days, 7)

    def test_near_band_cycles_faster_than_far_band(self):
        """The whole point of banding: near dates get revisited sooner."""
        cfg = self.bands_config()
        cursors = {}
        near_wraps = far_wraps = 0
        previous = (0, 0)
        for _ in range(15):
            _, cursors = plan_slice(cfg, cursors, today=TODAY)
            if cursors["0"] < previous[0]:
                near_wraps += 1
            if cursors["2"] < previous[1]:
                far_wraps += 1
            previous = (cursors["0"], cursors["2"])
        self.assertGreater(near_wraps, far_wraps)

    def test_cursors_for_removed_bands_are_dropped(self):
        """Switching to a flat rotation shouldn't leave orphan cursors in state."""
        cfg = make_config(
            search={"min_days_ahead": 0, "window_days": 14, "trip_nights": [3]},
            source={"pairs_per_run": 4, "bands": []},
        )
        _, cursors = plan_slice(cfg, {"0": 2, "1": 9, "2": 4, "3": 7}, today=TODAY)
        self.assertEqual(sorted(cursors), ["0"])

    def test_existing_cursor_position_is_resumed(self):
        cfg = make_config(
            search={"min_days_ahead": 0, "window_days": 14, "trip_nights": [3]},
            source={"pairs_per_run": 4, "bands": []},
        )
        picked, _ = plan_slice(cfg, {"0": 4}, today=TODAY)
        expected, _ = plan_slice(cfg, {"0": 4}, today=TODAY)
        self.assertEqual(picked, expected)
        first, _ = plan_slice(cfg, {}, today=TODAY)
        self.assertNotEqual(picked, first)

    def test_no_bands_falls_back_to_one_rotation(self):
        cfg = make_config(
            search={"min_days_ahead": 0, "window_days": 14, "trip_nights": [3]},
            source={"pairs_per_run": 4, "bands": []},
        )
        picked, cursors = plan_slice(cfg, {}, today=TODAY)
        self.assertEqual(len(picked), 4)
        self.assertEqual(cursors, {"0": 4})


class TestMockSource(unittest.TestCase):
    def test_deterministic(self):
        cfg = make_config(source={"pairs_per_run": 5})
        first, cursors = MockSource(cfg).sweep({})
        second, _ = MockSource(cfg).sweep({})
        self.assertEqual([o.price for o in first], [o.price for o in second])
        self.assertTrue(cursors)

    def test_emits_both_stop_classes_when_connections_allowed(self):
        cfg = make_config(source={"pairs_per_run": 5}, search={"max_stops": 1})
        offers, _ = MockSource(cfg).sweep({})
        self.assertEqual(len(offers), 10)
        self.assertEqual(sorted({o.stops for o in offers}), [0, 1])

    def test_nonstop_only_when_configured(self):
        cfg = make_config(source={"pairs_per_run": 5}, search={"max_stops": 0})
        offers, _ = MockSource(cfg).sweep({})
        self.assertEqual(len(offers), 5)
        self.assertTrue(all(o.stops == 0 for o in offers))


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
