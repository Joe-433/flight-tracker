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

    def test_percentile_alone_does_not_alert(self):
        """Bottom 5% is recorded and explained, but never pushes a message."""
        cfg = make_config(
            alerts={"threshold_usd": 100}, deals={"cheap_percentile": 0.05}
        )
        state = State()
        needed = analysis.min_route_samples(0.05)
        state.observations["2026-09-25|2026-09-29"] = [
            [NOW.isoformat(), 700 + i] for i in range(needed)
        ]
        result = analysis.assess([make_offer(300)], state, cfg, now=NOW)
        self.assertEqual(result.deals[0].reasons, ["percentile"])
        self.assertEqual(result.alerts, [])

    def test_threshold_is_the_only_trigger(self):
        """A cheap-percentile fare under the threshold alerts; over it doesn't."""
        cfg = make_config(
            alerts={"threshold_usd": 250}, deals={"cheap_percentile": 0.05}
        )
        state = State()
        state.observations["2026-09-25|2026-09-29"] = [
            [NOW.isoformat(), 700 + i]
            for i in range(analysis.min_route_samples(0.05))
        ]
        over = analysis.assess([make_offer(300)], state, cfg, now=NOW)
        self.assertEqual(over.alerts, [])
        under = analysis.assess([make_offer(240)], state, cfg, now=NOW)
        self.assertEqual(len(under.alerts), 1)
        self.assertIn("threshold", under.alerts[0].reasons)
        self.assertIn("percentile", under.alerts[0].reasons)

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


class TestStopThresholds(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(
            alerts={"threshold_usd": 250, "threshold_usd_with_stops": 200}
        )

    def test_nonstop_uses_the_higher_bar(self):
        self.assertEqual(
            analysis.threshold_for(make_offer(240, stops=0), self.cfg), 250
        )

    def test_connection_uses_the_lower_bar(self):
        self.assertEqual(
            analysis.threshold_for(make_offer(240, stops=1), self.cfg), 200
        )

    def test_connection_between_the_two_bars_stays_silent(self):
        """$240 with a layover is not worth waking up for; nonstop is."""
        stopped = analysis.assess([make_offer(240, stops=1)], State(), self.cfg, now=NOW)
        self.assertEqual(stopped.alerts, [])
        direct = analysis.assess([make_offer(240, stops=0)], State(), self.cfg, now=NOW)
        self.assertEqual(len(direct.alerts), 1)

    def test_cheap_connection_alerts(self):
        result = analysis.assess([make_offer(189, stops=1)], State(), self.cfg, now=NOW)
        self.assertEqual(len(result.alerts), 1)

    def test_unknown_stop_count_is_treated_as_connecting(self):
        offer = make_offer(240)
        offer.stops = None
        self.assertEqual(analysis.threshold_for(offer, self.cfg), 200)

    def test_stop_classes_track_separate_histories(self):
        """A $210 one-stop and a $390 nonstop must not average together."""
        direct = make_offer(390, stops=0)
        stopped = make_offer(210, stops=1)
        self.assertNotEqual(direct.key, stopped.key)
        self.assertEqual(direct.key, "2026-09-20|2026-09-24")
        self.assertEqual(stopped.key, "2026-09-20|2026-09-24|1")


class TestDayStats(unittest.TestCase):
    def test_reads_stop_count_from_the_key(self):
        state = State()
        state.observations["2026-09-20|2026-09-24|1"] = [[NOW.isoformat(), 190]]
        state.observations["2026-09-20|2026-09-24"] = [[NOW.isoformat(), 380]]
        stats = analysis.day_stats([], state, None, now=NOW)
        self.assertEqual(stats[0].price, 190)
        self.assertEqual(stats[0].stops, 1)

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
