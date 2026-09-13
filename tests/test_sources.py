from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.sources.base import date_pairs, slice_for_run
from flight_tracker.sources.mock import MockSource

TODAY = dt.date(2026, 9, 12)


class TestDatePairs(unittest.TestCase):
    def test_return_stays_inside_window(self):
        cfg = make_config(search={"window_days": 14, "trip_nights": [3, 7]})
        pairs = date_pairs(cfg, today=TODAY)
        end = TODAY + dt.timedelta(days=14)
        for out_date, ret_date in pairs:
            self.assertLessEqual(dt.date.fromisoformat(ret_date), end)
            self.assertGreaterEqual(dt.date.fromisoformat(out_date), TODAY)

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


class TestMockSource(unittest.TestCase):
    def test_deterministic_and_nonstop(self):
        cfg = make_config(source={"pairs_per_run": 5})
        first, cursor = MockSource(cfg).sweep(0)
        second, _ = MockSource(cfg).sweep(0)
        self.assertEqual([o.price for o in first], [o.price for o in second])
        self.assertEqual(cursor, 5)
        self.assertTrue(all(o.stops == 0 for o in first))


if __name__ == "__main__":
    unittest.main()
