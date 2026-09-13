from __future__ import annotations

import datetime as dt
import unittest

from helpers import NOW, make_config, make_offer

from flight_tracker import analysis
from flight_tracker.state import State


def seeded_state(key: str, prices, start: dt.datetime = NOW) -> State:
    state = State()
    state.observations[key] = [
        [(start - dt.timedelta(hours=6 * i)).isoformat(), price]
        for i, price in enumerate(prices)
    ]
    return state


class TestMinRouteSamples(unittest.TestCase):
    def test_tighter_percentile_needs_more_history(self):
        self.assertEqual(analysis.min_route_samples(0.05), 60)
        self.assertEqual(analysis.min_route_samples(0.20), 20)

    def test_never_below_floor(self):
        self.assertEqual(analysis.min_route_samples(0.9), 20)
        self.assertEqual(analysis.min_route_samples(0), 20)


class TestPercentile(unittest.TestCase):
    def test_interpolates(self):
        self.assertEqual(analysis.percentile([10, 20, 30, 40], 0.0), 10)
        self.assertEqual(analysis.percentile([10, 20, 30, 40], 1.0), 40)
        self.assertEqual(analysis.percentile([10, 20, 30, 40], 0.5), 25)

    def test_empty(self):
        self.assertIsNone(analysis.percentile([], 0.5))
        self.assertIsNone(analysis.median([]))


class TestAssess(unittest.TestCase):
    def test_threshold_alone_alerts(self):
        cfg = make_config(alerts={"threshold_usd": 250})
        result = analysis.assess([make_offer(199)], State(), cfg, now=NOW)
        self.assertEqual(len(result.alerts), 1)
        self.assertEqual(result.alerts[0].reasons, ["threshold"])

    def test_expensive_fare_is_silent(self):
        cfg = make_config(alerts={"threshold_usd": 250})
        result = analysis.assess([make_offer(600)], State(), cfg, now=NOW)
        self.assertEqual(result.alerts, [])

    def test_baseline_alone_does_not_alert(self):
        """A 15% drop from an absurd price is still an absurd price."""
        cfg = make_config(
            alerts={"threshold_usd": 100}, deals={"min_observations": 3}
        )
        offer = make_offer(400)
        state = seeded_state(offer.key, [500, 510, 505, 500])
        result = analysis.assess([offer], state, cfg, now=NOW)
        self.assertEqual(result.deals[0].reasons, ["baseline"])
        self.assertEqual(result.alerts, [])

    def test_percentile_alone_alerts(self):
        """Bottom 5% of the route is an alert on its own, no threshold needed."""
        cfg = make_config(
            alerts={"threshold_usd": 100}, deals={"cheap_percentile": 0.05}
        )
        state = State()
        needed = analysis.min_route_samples(0.05)
        state.observations["2026-09-25|2026-09-29"] = [
            [NOW.isoformat(), 700 + i] for i in range(needed)
        ]
        result = analysis.assess([make_offer(300)], state, cfg, now=NOW)
        self.assertEqual(result.alerts[0].reasons, ["percentile"])

    def test_percentile_silent_until_enough_history(self):
        cfg = make_config(
            alerts={"threshold_usd": 100}, deals={"cheap_percentile": 0.05}
        )
        state = State()
        state.observations["2026-09-25|2026-09-29"] = [
            [NOW.isoformat(), 700 + i]
            for i in range(analysis.min_route_samples(0.05) - 1)
        ]
        result = analysis.assess([make_offer(300)], state, cfg, now=NOW)
        self.assertEqual(result.alerts, [])

    def test_baseline_needs_enough_observations(self):
        cfg = make_config(alerts={"threshold_usd": 100}, deals={"min_observations": 6})
        offer = make_offer(400)
        state = seeded_state(offer.key, [500, 510])
        result = analysis.assess([offer], state, cfg, now=NOW)
        self.assertEqual(result.deals, [])

    def test_current_fare_excluded_from_its_own_baseline(self):
        """Recording happens after assessment; verify the contract holds."""
        cfg = make_config(alerts={"threshold_usd": 100}, deals={"min_observations": 3})
        offer = make_offer(400)
        state = seeded_state(offer.key, [500, 500, 500])
        result = analysis.assess([offer], state, cfg, now=NOW)
        self.assertEqual(result.deals[0].baseline, 500)


class TestDayStats(unittest.TestCase):
    def test_picks_cheapest_per_departure_day(self):
        state = State()
        state.observations["2026-09-20|2026-09-24"] = [[NOW.isoformat(), 300]]
        state.observations["2026-09-20|2026-09-25"] = [[NOW.isoformat(), 275]]
        state.observations["2026-09-21|2026-09-25"] = [[NOW.isoformat(), 410]]
        stats = analysis.day_stats([], state, cheap_cutoff=280, now=NOW)
        self.assertEqual([s.out_date for s in stats], ["2026-09-20", "2026-09-21"])
        self.assertEqual(stats[0].price, 275)
        self.assertEqual(stats[0].ret_date, "2026-09-25")
        self.assertTrue(stats[0].cheap)
        self.assertFalse(stats[1].cheap)

    def test_past_departures_dropped(self):
        state = State()
        state.observations["2026-09-01|2026-09-05"] = [[NOW.isoformat(), 99]]
        self.assertEqual(analysis.day_stats([], state, None, now=NOW), [])

    def test_fresh_offers_merge_with_history(self):
        state = State()
        state.observations["2026-09-20|2026-09-24"] = [[NOW.isoformat(), 300]]
        stats = analysis.day_stats(
            [make_offer(210, "2026-09-20", nights=4)], state, None, now=NOW
        )
        self.assertEqual(stats[0].price, 210)


if __name__ == "__main__":
    unittest.main()
