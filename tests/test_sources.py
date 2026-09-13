from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.config import Band
from flight_tracker.sources.base import date_pairs, plan_slice, slice_for_run
from flight_tracker.sources.mock import MockSource

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

    def test_no_bands_falls_back_to_one_rotation(self):
        cfg = make_config(
            search={"min_days_ahead": 0, "window_days": 14, "trip_nights": [3]},
            source={"pairs_per_run": 4, "bands": []},
        )
        picked, cursors = plan_slice(cfg, {}, today=TODAY)
        self.assertEqual(len(picked), 4)
        self.assertEqual(cursors, {"0": 4})


class TestMockSource(unittest.TestCase):
    def test_deterministic_and_nonstop(self):
        cfg = make_config(source={"pairs_per_run": 5})
        first, cursors = MockSource(cfg).sweep({})
        second, _ = MockSource(cfg).sweep({})
        self.assertEqual([o.price for o in first], [o.price for o in second])
        self.assertEqual(len(first), 5)
        self.assertTrue(cursors)
        self.assertTrue(all(o.stops == 0 for o in first))


if __name__ == "__main__":
    unittest.main()
