from __future__ import annotations

import datetime as dt
import unittest

from helpers import NOW, make_config, make_offer

from flight_tracker import analysis
from flight_tracker.state import State


class TestRecordLows(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(alerts={"record_low": True, "record_min_drop_usd": 5})

    def claim(self, state, offers, now=NOW):
        return analysis.claim_record_lows(offers, state, self.cfg, now=now)

    def test_first_sighting_arms_silently(self):
        """Alerting here would mean alerting on the first thing we ever saw."""
        state = State()
        self.assertEqual(self.claim(state, [make_offer(400)]), [])
        self.assertEqual(state.records["nonstop"]["price"], 400)

    def test_new_low_alerts_and_raises_the_bar(self):
        state = State()
        self.claim(state, [make_offer(400)])
        found = self.claim(state, [make_offer(350)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].previous, 400)
        self.assertEqual(found[0].saving, 50)
        self.assertEqual(state.records["nonstop"]["price"], 350)

    def test_same_low_does_not_realert(self):
        """Self-limiting by construction: each alert raises its own bar."""
        state = State()
        self.claim(state, [make_offer(400)])
        self.claim(state, [make_offer(350)])
        self.assertEqual(self.claim(state, [make_offer(350)]), [])

    def test_trivial_improvement_is_ignored(self):
        state = State()
        self.claim(state, [make_offer(400)])
        self.assertEqual(self.claim(state, [make_offer(397)]), [])
        self.assertEqual(len(self.claim(state, [make_offer(395)])), 1)

    def test_higher_price_never_alerts(self):
        state = State()
        self.claim(state, [make_offer(400)])
        self.assertEqual(self.claim(state, [make_offer(600)]), [])
        self.assertEqual(state.records["nonstop"]["price"], 400)

    def test_stop_classes_have_independent_records(self):
        state = State()
        self.claim(state, [make_offer(400, stops=0), make_offer(300, stops=1)])
        self.assertEqual(state.records["nonstop"]["price"], 400)
        self.assertEqual(state.records["connecting"]["price"], 300)
        found = self.claim(state, [make_offer(380, stops=0)])
        self.assertEqual(len(found), 1)
        self.assertEqual(analysis.stop_class(found[0].offer), "nonstop")

    def test_only_the_cheapest_of_a_class_counts(self):
        state = State()
        self.claim(state, [make_offer(400)])
        found = self.claim(
            state, [make_offer(390, "2026-09-21"), make_offer(340, "2026-09-22")]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].offer.price, 340)

    def test_records_expire_with_the_history_window(self):
        """A winter fluke shouldn't set the bar forever."""
        state = State()
        self.claim(state, [make_offer(200)])
        later = NOW + dt.timedelta(days=45)
        self.assertEqual(self.claim(state, [make_offer(400)], now=later), [])
        self.assertEqual(state.records["nonstop"]["price"], 400)

    def test_disabled_by_config(self):
        self.cfg.alerts.record_low = False
        state = State()
        self.claim(state, [make_offer(400)])
        self.assertEqual(state.records, {})

    def test_survives_a_save_and_load(self):
        import os
        import tempfile

        state = State()
        self.claim(state, [make_offer(400)])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state.save(path)
            loaded = State.load(path)
        self.assertEqual(loaded.records["nonstop"]["price"], 400)
        self.assertEqual(self.claim(loaded, [make_offer(300)])[0].previous, 400)


if __name__ == "__main__":
    unittest.main()
