from __future__ import annotations

import datetime as dt
import unittest

from helpers import NOW, make_config

from flight_tracker import deadman
from flight_tracker.state import State


class TestDeadman(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(
            deadman={"fail_runs": 2, "stale_hours": 6, "renotify_hours": 24}
        )

    def test_healthy_run_is_quiet(self):
        state = State()
        health = deadman.update(state, self.cfg, got_data=True, now=NOW)
        self.assertFalse(health.down)
        self.assertFalse(health.should_notify)
        self.assertEqual(state.last_data, NOW.isoformat())

    def test_single_failure_is_tolerated(self):
        state = State()
        state.last_data = NOW.isoformat()
        health = deadman.update(state, self.cfg, got_data=False, now=NOW)
        self.assertFalse(health.down)
        self.assertEqual(state.consecutive_failures, 1)

    def test_second_consecutive_failure_fires(self):
        state = State()
        state.last_data = NOW.isoformat()
        deadman.update(state, self.cfg, got_data=False, now=NOW)
        health = deadman.update(
            state, self.cfg, got_data=False, now=NOW + dt.timedelta(minutes=30)
        )
        self.assertTrue(health.down)
        self.assertTrue(health.should_notify)
        self.assertTrue(state.deadman["down"])

    def test_staleness_fires_even_with_one_failure(self):
        state = State()
        state.last_data = (NOW - dt.timedelta(hours=9)).isoformat()
        health = deadman.update(state, self.cfg, got_data=False, now=NOW)
        self.assertTrue(health.down)
        self.assertTrue(any("9.0 hours" in r for r in health.reasons))

    def test_does_not_renotify_immediately(self):
        state = State()
        state.last_data = NOW.isoformat()
        deadman.update(state, self.cfg, got_data=False, now=NOW)
        deadman.update(state, self.cfg, got_data=False, now=NOW + dt.timedelta(hours=1))
        health = deadman.update(
            state, self.cfg, got_data=False, now=NOW + dt.timedelta(hours=2)
        )
        self.assertTrue(health.down)
        self.assertFalse(health.should_notify)

    def test_renotifies_after_window(self):
        state = State()
        state.last_data = NOW.isoformat()
        deadman.update(state, self.cfg, got_data=False, now=NOW)
        deadman.update(state, self.cfg, got_data=False, now=NOW + dt.timedelta(hours=1))
        health = deadman.update(
            state, self.cfg, got_data=False, now=NOW + dt.timedelta(hours=26)
        )
        self.assertTrue(health.should_notify)

    def test_recovery_notifies_once(self):
        state = State()
        state.last_data = NOW.isoformat()
        deadman.update(state, self.cfg, got_data=False, now=NOW)
        deadman.update(state, self.cfg, got_data=False, now=NOW + dt.timedelta(hours=1))
        recovered = deadman.update(
            state, self.cfg, got_data=True, now=NOW + dt.timedelta(hours=2)
        )
        self.assertTrue(recovered.recovered)
        self.assertTrue(recovered.should_notify)
        quiet = deadman.update(
            state, self.cfg, got_data=True, now=NOW + dt.timedelta(hours=3)
        )
        self.assertFalse(quiet.should_notify)

    def test_never_fetched_anything(self):
        state = State()
        deadman.update(state, self.cfg, got_data=False, now=NOW)
        health = deadman.update(state, self.cfg, got_data=False, now=NOW)
        self.assertTrue(health.down)
        self.assertTrue(any("never" in r for r in health.reasons))


if __name__ == "__main__":
    unittest.main()
