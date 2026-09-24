from __future__ import annotations

import argparse
import datetime as dt
import json
import unittest
from unittest import mock

from helpers import make_config

from flight_tracker import cli, pipeline
from flight_tracker.sources.grid import GridUnavailable, combos
from flight_tracker.state import State, utcnow


def mock_cfg(**extra):
    base = {
        "source": {"backend": "mock", "drills_per_run": 8, "shards": 4},
        "search": {"min_days_ahead": 14, "window_days": 20, "trip_nights": [5, 6]},
    }
    for section, values in extra.items():
        base.setdefault(section, {}).update(values)
    cfg = make_config(**base)
    cfg.route.destinations = ["LAX", "ONT"]
    return cfg


def quiet_args():
    return argparse.Namespace(no_notify=True, console=False, fail_on_down=False)


class TestPlanStage(unittest.TestCase):
    def test_scans_the_calendar_and_splits_the_drills(self):
        cfg = mock_cfg()
        doc = pipeline.make_plan(cfg, State())
        self.assertEqual(doc["grid_status"], "ok")
        self.assertEqual(len(doc["grid"]), len(combos(cfg)))
        self.assertEqual(len(doc["shards"]), 4)
        self.assertEqual(sum(len(s) for s in doc["shards"]), 8)

    def test_plan_survives_a_json_round_trip(self):
        """It's handed between GitHub jobs as an artifact."""
        doc = pipeline.make_plan(mock_cfg(), State())
        self.assertEqual(json.loads(json.dumps(doc)), doc)

    def test_fresh_scans_are_not_repeated(self):
        cfg = mock_cfg()
        state = State()
        first = pipeline.make_plan(cfg, state)
        state.grid.update(first["grid"])
        second = pipeline.make_plan(cfg, state)
        self.assertEqual(second["grid_status"], "skipped")
        self.assertEqual(second["grid_scans"], 0)

    def test_stale_scans_are_repeated(self):
        cfg = mock_cfg()
        state = State()
        old = (utcnow() - dt.timedelta(hours=2)).isoformat()
        for key, entry in pipeline.make_plan(cfg, state)["grid"].items():
            state.grid[key] = dict(entry, seen=old)
        self.assertEqual(pipeline.make_plan(cfg, state)["grid_status"], "ok")

    def test_missing_library_degrades_instead_of_failing(self):
        cfg = mock_cfg()
        with mock.patch.object(
            pipeline, "get_grid", side_effect=GridUnavailable("no fli")
        ):
            doc = pipeline.make_plan(cfg, State())
        self.assertEqual(doc["grid_status"], "unavailable")
        self.assertEqual(sum(len(s) for s in doc["shards"]), 8)  # still drills

    def test_disabled_grid(self):
        doc = pipeline.make_plan(mock_cfg(grid={"enabled": False}), State())
        self.assertEqual(doc["grid_status"], "disabled")
        self.assertEqual(doc["grid"], {})


class TestDrillStage(unittest.TestCase):
    def test_each_shard_searches_only_its_items(self):
        cfg = mock_cfg()
        doc = pipeline.make_plan(cfg, State())
        seen = []
        for shard in range(4):
            result = pipeline.run_drills(cfg, doc, shard)
            self.assertEqual(len(result["checked"]), len(doc["shards"][shard]))
            seen.extend(result["checked"])
        self.assertEqual(len(seen), len(set(seen)), "a date pair was searched twice")

    def test_a_shard_past_the_end_is_empty_not_an_error(self):
        result = pipeline.run_drills(mock_cfg(), {"shards": [[]]}, 3)
        self.assertEqual(result["offers"], [])


class TestApplyStage(unittest.TestCase):
    def run_all(self, cfg, state):
        doc = pipeline.make_plan(cfg, state)
        results = [pipeline.run_drills(cfg, doc, s) for s in range(len(doc["shards"]))]
        cli._apply(cfg, state, doc, results, quiet_args())
        return doc

    def test_full_cycle_populates_state(self):
        cfg = mock_cfg()
        state = State()
        self.run_all(cfg, state)
        self.assertEqual(len(state.grid), len(combos(cfg)))
        self.assertEqual(len(state.checked), 8)
        self.assertEqual(len(state.runs), 1)
        self.assertTrue(state.latest)
        self.assertEqual(state.consecutive_failures, 0)

    def test_second_run_searches_different_items(self):
        """The planner's memory works: it doesn't just repeat itself."""
        cfg = mock_cfg()
        state = State()
        first = self.run_all(cfg, state)
        second = self.run_all(cfg, state)
        a = {tuple(i) for shard in first["shards"] for i in shard}
        b = {tuple(i) for shard in second["shards"] for i in shard}
        self.assertFalse(a & b)

    def test_full_searches_remember_the_calendar_price(self):
        """The planner's movement signal depends on this stamp."""
        cfg = mock_cfg()
        state = State()
        self.run_all(cfg, state)
        stamped = [snap for snap in state.latest.values() if "grid_at" in snap]
        self.assertTrue(stamped)
        self.assertTrue(all(isinstance(snap["grid_at"], (int, float)) for snap in stamped))

    def test_missing_plan_and_results_count_as_a_failed_run(self):
        """A crashed plan job must still register with the dead man's switch."""
        cfg = mock_cfg()
        state = State()
        cli._apply(cfg, state, {}, [], quiet_args())
        self.assertEqual(state.consecutive_failures, 1)


class TestCliParsing(unittest.TestCase):
    def test_probe_runs_with_its_own_flags(self):
        """Regression: probe read an args.bags its parser never defined."""
        args = cli.build_parser().parse_args(
            ["probe", "--min-days", "30", "--carry-on", "0", "--pairs", "1"]
        )
        with mock.patch.object(cli, "get_source", side_effect=cli.ScrapeError("offline")):
            self.assertEqual(args.func(args), 1)  # fails cleanly, doesn't crash


class TestRunCounter(unittest.TestCase):
    def test_a_run_that_just_happened_counts(self):
        """0.0 hours old is falsy; it must still count."""
        state = State()
        now = utcnow()
        state.runs = [now.isoformat()]
        self.assertEqual(cli.runs_last_day(state, now), 1)

    def test_old_runs_do_not(self):
        state = State()
        now = utcnow()
        state.runs = [(now - dt.timedelta(hours=30)).isoformat(), now.isoformat()]
        self.assertEqual(cli.runs_last_day(state, now), 1)


class TestGridHealth(unittest.TestCase):
    def test_notice_after_repeated_failures_then_recovery(self):
        cfg = mock_cfg(grid={"notify_after_failures": 3})
        state = State()
        notices = [cli._update_grid_health(state, cfg, "failed") for _ in range(4)]
        self.assertEqual([n is not None for n in notices], [False, False, True, False])
        self.assertTrue(state.grid_health["down"])
        back = cli._update_grid_health(state, cfg, "ok")
        self.assertIsNotNone(back)
        self.assertEqual(state.grid_health, {"failures": 0, "down": False})

    def test_skipped_scans_are_not_evidence(self):
        cfg = mock_cfg()
        state = State()
        cli._update_grid_health(state, cfg, "failed")
        cli._update_grid_health(state, cfg, "skipped")
        self.assertEqual(state.grid_health["failures"], 1)


if __name__ == "__main__":
    unittest.main()
