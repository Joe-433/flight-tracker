from __future__ import annotations

import datetime as dt
import unittest

from helpers import NOW, make_config, make_offer

from flight_tracker.state import State


def stamped(offset_hours: float) -> str:
    return (NOW + dt.timedelta(hours=offset_hours)).isoformat()


class TestMerge(unittest.TestCase):
    """Two overlapping sweeps each write the whole file; neither may be lost."""

    def test_observations_are_unioned(self):
        a, b = State(), State()
        a.observations["k"] = [[stamped(0), 300]]
        b.observations["k"] = [[stamped(1), 280]]
        a.merge(b)
        self.assertEqual([p[1] for p in a.observations["k"]], [300, 280])

    def test_identical_points_are_not_duplicated(self):
        a, b = State(), State()
        a.observations["k"] = [[stamped(0), 300]]
        b.observations["k"] = [[stamped(0), 300]]
        a.merge(b)
        self.assertEqual(len(a.observations["k"]), 1)

    def test_points_stay_in_time_order(self):
        a, b = State(), State()
        a.observations["k"] = [[stamped(2), 300]]
        b.observations["k"] = [[stamped(0), 280], [stamped(1), 290]]
        a.merge(b)
        stamps = [str(p[0]) for p in a.observations["k"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_keys_only_the_other_side_has_are_kept(self):
        a, b = State(), State()
        a.observations["mine"] = [[stamped(0), 1]]
        b.observations["theirs"] = [[stamped(0), 2]]
        a.merge(b)
        self.assertEqual(sorted(a.observations), ["mine", "theirs"])

    def test_freshest_snapshot_wins(self):
        a, b = State(), State()
        a.latest["k"] = {"price": 300, "seen": stamped(0)}
        b.latest["k"] = {"price": 280, "seen": stamped(1)}
        a.merge(b)
        self.assertEqual(a.latest["k"]["price"], 280)

    def test_stale_snapshot_does_not_overwrite(self):
        a, b = State(), State()
        a.latest["k"] = {"price": 280, "seen": stamped(1)}
        b.latest["k"] = {"price": 300, "seen": stamped(0)}
        a.merge(b)
        self.assertEqual(a.latest["k"]["price"], 280)

    def test_lowest_record_wins_regardless_of_who_saw_it(self):
        a, b = State(), State()
        a.records["nonstop"] = {"price": 357, "seen": stamped(1)}
        b.records["nonstop"] = {"price": 300, "seen": stamped(0)}
        a.merge(b)
        self.assertEqual(a.records["nonstop"]["price"], 300)

    def test_most_recent_alert_wins_so_cooldowns_survive(self):
        a, b = State(), State()
        a.alerts["k"] = {"price": 240, "ts": stamped(0)}
        b.alerts["k"] = {"price": 230, "ts": stamped(2)}
        a.merge(b)
        self.assertEqual(a.alerts["k"]["ts"], stamped(2))

    def test_furthest_cursor_wins(self):
        a, b = State(), State()
        a.cursors = {"0": 10, "alt": 3}
        b.cursors = {"0": 40, "1": 7}
        a.merge(b)
        self.assertEqual(a.cursors, {"0": 40, "alt": 3, "1": 7})

    def test_a_success_anywhere_clears_the_failure_streak(self):
        a, b = State(), State()
        a.consecutive_failures = 3
        b.consecutive_failures = 0
        b.last_data = stamped(1)
        a.merge(b)
        self.assertEqual(a.consecutive_failures, 0)
        self.assertEqual(a.last_data, stamped(1))

    def test_merging_empty_state_changes_nothing(self):
        a = State()
        a.record([make_offer(300)], make_config().history, now=NOW)
        before = dict(a.observations)
        a.merge(State())
        self.assertEqual(a.observations, before)

    def test_merge_is_idempotent(self):
        a, b = State(), State()
        a.observations["k"] = [[stamped(0), 300]]
        b.observations["k"] = [[stamped(1), 280]]
        a.merge(b)
        a.merge(b)
        self.assertEqual(len(a.observations["k"]), 2)


if __name__ == "__main__":
    unittest.main()
