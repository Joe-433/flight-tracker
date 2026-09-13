from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest

from helpers import NOW, make_config, make_offer

from flight_tracker.state import State


class TestPersistence(unittest.TestCase):
    def test_roundtrip(self):
        state = State()
        state.record([make_offer(300)], None, now=NOW)
        state.cursors = {"0": 7, "1": 3}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "state.json")
            state.save(path)
            loaded = State.load(path)
        self.assertEqual(loaded.cursors, {"0": 7, "1": 3})
        self.assertEqual(loaded.observations, state.observations)

    def test_missing_file_is_fresh_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = State.load(os.path.join(tmp, "absent.json"))
        self.assertEqual(loaded.observations, {})


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.history = make_config().history

    def test_skips_unchanged_price_within_resample_window(self):
        state = State()
        offer = make_offer(300)
        self.assertEqual(state.record([offer], self.history, now=NOW), 1)
        self.assertEqual(
            state.record([offer], self.history, now=NOW + dt.timedelta(hours=1)), 0
        )
        self.assertEqual(len(state.observations[offer.key]), 1)

    def test_records_changed_price_immediately(self):
        state = State()
        state.record([make_offer(300)], self.history, now=NOW)
        state.record([make_offer(280)], self.history, now=NOW + dt.timedelta(minutes=15))
        self.assertEqual(len(state.observations["2026-09-20|2026-09-24"]), 2)

    def test_ignores_movement_below_min_change(self):
        """$2 of noise on 400 tracked pairs is pure state-file churn."""
        state = State()
        state.record([make_offer(300)], self.history, now=NOW)
        state.record([make_offer(298)], self.history, now=NOW + dt.timedelta(minutes=15))
        self.assertEqual(len(state.observations["2026-09-20|2026-09-24"]), 1)

    def test_records_unchanged_price_after_resample_window(self):
        state = State()
        offer = make_offer(300)
        state.record([offer], self.history, now=NOW)
        state.record([offer], self.history, now=NOW + dt.timedelta(hours=13))
        self.assertEqual(len(state.observations[offer.key]), 2)

    def test_caps_points_per_pair(self):
        state = State()
        history = make_config(history={"max_points_per_pair": 5}).history
        for i in range(20):
            state.record(
                [make_offer(300 + i * 10)], history, now=NOW + dt.timedelta(hours=i)
            )
        self.assertEqual(len(state.observations["2026-09-20|2026-09-24"]), 5)


class TestTrim(unittest.TestCase):
    def test_drops_departed_trips_and_old_points(self):
        state = State()
        state.observations["2026-09-01|2026-09-05"] = [[NOW.isoformat(), 100]]
        state.observations["2026-09-20|2026-09-24"] = [
            [(NOW - dt.timedelta(days=40)).isoformat(), 400],
            [NOW.isoformat(), 380],
        ]
        state.alerts["2026-09-01|2026-09-05"] = {"price": 100, "ts": NOW.isoformat()}
        state.trim(history_days=30, now=NOW)
        self.assertNotIn("2026-09-01|2026-09-05", state.observations)
        self.assertNotIn("2026-09-01|2026-09-05", state.alerts)
        self.assertEqual(state.observations["2026-09-20|2026-09-24"], [[NOW.isoformat(), 380]])


class TestScopePruning(unittest.TestCase):
    def test_drops_trip_lengths_no_longer_searched(self):
        """Narrowing trip_nights must not leave zombies in the report."""
        state = State()
        state.observations["2026-09-20|2026-09-23"] = [[NOW.isoformat(), 278]]  # 3n
        state.observations["2026-09-20|2026-09-25"] = [[NOW.isoformat(), 400]]  # 5n
        state.latest["2026-09-20|2026-09-23"] = {"price": 278}
        state.latest["2026-09-20|2026-09-25"] = {"price": 400}
        state.trim(30, now=NOW, allowed_nights=[5, 6, 7])
        self.assertEqual(list(state.observations), ["2026-09-20|2026-09-25"])
        self.assertEqual(list(state.latest), ["2026-09-20|2026-09-25"])

    def test_connecting_keys_are_pruned_too(self):
        state = State()
        state.observations["2026-09-20|2026-09-23|1"] = [[NOW.isoformat(), 200]]
        state.trim(30, now=NOW, allowed_nights=[5, 6, 7])
        self.assertEqual(state.observations, {})

    def test_no_pruning_without_an_allowed_set(self):
        state = State()
        state.observations["2026-09-20|2026-09-23"] = [[NOW.isoformat(), 278]]
        state.trim(30, now=NOW)
        self.assertIn("2026-09-20|2026-09-23", state.observations)


class TestAlertDedup(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(
            alerts={"cooldown_hours": 12, "rebeat_drop_usd": 15}
        )

    def test_first_sighting_alerts(self):
        self.assertTrue(State().should_alert(make_offer(200), self.cfg, now=NOW))

    def test_same_price_inside_cooldown_is_silent(self):
        state = State()
        offer = make_offer(200)
        state.mark_alerted(offer, now=NOW)
        later = NOW + dt.timedelta(hours=3)
        self.assertFalse(state.should_alert(offer, self.cfg, now=later))

    def test_further_drop_breaks_cooldown(self):
        state = State()
        state.mark_alerted(make_offer(200), now=NOW)
        later = NOW + dt.timedelta(hours=3)
        self.assertFalse(state.should_alert(make_offer(190), self.cfg, now=later))
        self.assertTrue(state.should_alert(make_offer(185), self.cfg, now=later))

    def test_alerts_again_after_cooldown(self):
        state = State()
        offer = make_offer(200)
        state.mark_alerted(offer, now=NOW)
        self.assertTrue(
            state.should_alert(offer, self.cfg, now=NOW + dt.timedelta(hours=13))
        )


if __name__ == "__main__":
    unittest.main()
