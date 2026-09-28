from __future__ import annotations

import argparse
import datetime as dt
import unittest
from unittest import mock

from helpers import make_config

from flight_tracker import cli
from flight_tracker.state import State

UTC = dt.timezone.utc


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


class TestReportSlot(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_monday_6am_pacific_in_daylight_time(self):
        # 2026-09-28 is a Monday; PDT is UTC-7, so 6am local is 13:00 UTC.
        self.assertEqual(cli.report_slot(self.cfg, at("2026-09-28T19:50:00")),
                         at("2026-09-28T13:00:00"))

    def test_monday_6am_pacific_in_standard_time(self):
        # 2026-11-16 is a Monday; PST is UTC-8, so 6am local is 14:00 UTC.
        self.assertEqual(cli.report_slot(self.cfg, at("2026-11-16T15:00:00")),
                         at("2026-11-16T14:00:00"))

    def test_before_the_slot_points_at_last_week(self):
        self.assertEqual(cli.report_slot(self.cfg, at("2026-09-28T12:59:00")),
                         at("2026-09-21T13:00:00"))

    def test_midweek_points_at_this_monday(self):
        self.assertEqual(cli.report_slot(self.cfg, at("2026-10-01T03:00:00")),
                         at("2026-09-28T13:00:00"))


class TestReportDue(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_never_sent_is_due(self):
        self.assertTrue(cli.report_due(State(), self.cfg, at("2026-09-28T19:50:00")))

    def test_sent_this_week_is_not(self):
        state = State(last_report="2026-09-28T13:20:00+00:00")
        self.assertFalse(cli.report_due(state, self.cfg, at("2026-10-02T10:00:00")))

    def test_last_weeks_send_leaves_this_week_due(self):
        """The case that bit: GitHub dropped Monday's trigger entirely."""
        state = State(last_report="2026-09-21T13:06:00+00:00")
        self.assertTrue(cli.report_due(state, self.cfg, at("2026-09-28T19:50:00")))

    def test_disabled(self):
        self.cfg.report.enabled = False
        self.assertFalse(cli.report_due(State(), self.cfg, at("2026-09-28T19:50:00")))


class TestApplySendsReport(unittest.TestCase):
    def run_apply(self, state, notify=True, delivered=True):
        cfg = make_config()
        args = argparse.Namespace(no_notify=not notify, console=False, fail_on_down=False)
        fake = mock.Mock()
        fake.send.return_value = [("discord", delivered, None)]
        with mock.patch.object(cli.Notifier, "from_env", return_value=fake):
            cli._apply(cfg, state, {}, [], args)
        return fake

    @staticmethod
    def reports(fake):
        return [c for c in fake.send.call_args_list
                if c.args[0].title.startswith("Cheapest")]

    def test_due_report_is_sent_once(self):
        state = State()
        fake = self.run_apply(state)
        self.assertEqual(len(self.reports(fake)), 1)
        self.assertIsNotNone(state.last_report)
        fake = self.run_apply(state)
        self.assertEqual(len(self.reports(fake)), 0)

    def test_failed_delivery_retries_next_run(self):
        state = State()
        self.run_apply(state, delivered=False)
        self.assertIsNone(state.last_report)

    def test_quiet_runs_neither_send_nor_mark(self):
        state = State()
        fake = self.run_apply(state, notify=False)
        self.assertEqual(len(self.reports(fake)), 0)
        self.assertIsNone(state.last_report)


class TestMergeLastReport(unittest.TestCase):
    def test_latest_send_wins(self):
        a = State(last_report="2026-09-21T13:00:00+00:00")
        b = State(last_report="2026-09-28T13:30:00+00:00")
        a.merge(b)
        self.assertEqual(a.last_report, "2026-09-28T13:30:00+00:00")


if __name__ == "__main__":
    unittest.main()
