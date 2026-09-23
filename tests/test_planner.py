from __future__ import annotations

import datetime as dt
import unittest

from helpers import NOW, make_config

from flight_tracker import planner
from flight_tracker.state import State


def cfg(**extra):
    base = {
        "search": {"min_days_ahead": 14, "window_days": 9, "trip_nights": [5]},
        "alerts": {"threshold_usd": 250, "threshold_usd_with_stops": 200},
        "source": {"drills_per_run": 5},
    }
    for section, values in extra.items():
        base.setdefault(section, {}).update(values)
    return make_config(**base)


def day(offset: int) -> str:
    return (NOW.date() + dt.timedelta(days=offset)).isoformat()


def item(offset: int, dest: str = "LAX", nights: int = 5):
    return (day(offset), day(offset + nights), dest)


def grid(prices_by_offset, dest="LAX", nights=5, stops=0):
    """{combo key: {departure date: price}}"""
    return {
        "%s|%d|%d" % (dest, nights, stops): {
            day(offset): price for offset, price in prices_by_offset.items()
        }
    }


def searched(state, it, hours_ago, price=None, stops=0):
    stamp = (NOW - dt.timedelta(hours=hours_ago)).isoformat()
    state.checked[planner.item_key(it)] = stamp
    if price is not None:
        state.latest["%s|%d" % (planner.item_key(it), stops)] = {
            "price": price, "seen": stamp,
        }


class TestUniverse(unittest.TestCase):
    def test_every_date_pair_for_every_destination(self):
        c = cfg(route={"destinations": ["LAX", "BUR"]})
        items = planner.universe(c, NOW.date())
        self.assertEqual(len(items), 10 * 2)  # 10 departure dates x 2 airports
        self.assertEqual({i[2] for i in items}, {"LAX", "BUR"})


class TestUrgency(unittest.TestCase):
    def test_calendar_under_the_alert_line_goes_first(self):
        c = cfg()
        state = State()
        for offset in range(14, 24):
            searched(state, item(offset), hours_ago=0.5)
        picks = planner.plan(
            c, state, budget=1, grid=grid({18: 240, 15: 400}), now=NOW
        )
        self.assertEqual(picks[0].item, item(18))
        self.assertIn("alert line", picks[0].reason)

    def test_connecting_fare_uses_its_own_lower_line(self):
        c = cfg()
        state = State()
        picks = planner.plan(
            c, state, budget=10, grid=grid({16: 230}, stops=1), now=NOW
        )
        reasons = {p.item: p.reason for p in picks}
        self.assertNotIn("alert line", reasons[item(16)])  # $230 > $200 one-stop line

    def test_new_record_is_urgent(self):
        c = cfg()
        state = State()
        state.records["nonstop"] = {"price": 357, "seen": NOW.isoformat()}
        picks = planner.plan(c, state, budget=10, grid=grid({20: 330}), now=NOW)
        reasons = {p.item: p.reason for p in picks}
        self.assertIn("record", reasons[item(20)])


def searched_with_grid(state, it, hours_ago, price, grid_at, stops=0):
    searched(state, it, hours_ago, price=price, stops=stops)
    state.latest["%s|%d" % (planner.item_key(it), stops)]["grid_at"] = grid_at


class TestEstimate(unittest.TestCase):
    def test_full_search_shifted_by_calendar_movement(self):
        # searched at $420 when the calendar said $400; calendar now $370
        self.assertEqual(planner.estimate(370, 420, 400), (390, 30))

    def test_no_calendar_baseline_trusts_the_full_search(self):
        self.assertEqual(planner.estimate(370, 420, None), (420, 0.0))

    def test_never_searched_uses_the_raw_calendar(self):
        self.assertEqual(planner.estimate(370, None, None), (370, 0.0))

    def test_nothing_known(self):
        self.assertEqual(planner.estimate(None, None, None), (None, 0.0))


class TestMovement(unittest.TestCase):
    def test_calendar_falling_since_the_last_search_counts_as_moved(self):
        c = cfg()
        state = State()
        searched_with_grid(state, item(15), hours_ago=2, price=420, grid_at=400)
        picks = planner.plan(c, state, budget=10, grid=grid({15: 380}), now=NOW)
        reasons = {p.item: p.reason for p in picks}
        self.assertIn("calendar down $20", reasons[item(15)])

    def test_calendar_bias_alone_is_not_movement(self):
        """The calendar sits ~$17 under full searches. Unchanged, that's not a drop."""
        c = cfg()
        state = State()
        searched_with_grid(state, item(15), hours_ago=2, price=420, grid_at=400)
        picks = planner.plan(c, state, budget=10, grid=grid({15: 400}), now=NOW)
        reasons = {p.item: p.reason for p in picks}
        self.assertNotIn("calendar down", reasons[item(15)])

    def test_refuted_record_does_not_stay_urgent(self):
        """Regression: calendar says $220 vs a $230 record, full search says $245.
        Before the fix this item was re-searched every run forever."""
        c = cfg()
        state = State()
        state.records["connecting"] = {"price": 230, "seen": NOW.isoformat()}
        searched_with_grid(state, item(15), hours_ago=0.5, price=245, grid_at=220, stops=1)
        picks = planner.plan(c, state, budget=10, grid=grid({15: 220}, stops=1), now=NOW)
        reasons = {p.item: p.reason for p in picks}
        self.assertNotIn("record", reasons[item(15)])

    def test_confirmed_drop_toward_the_record_is_urgent(self):
        c = cfg()
        state = State()
        state.records["connecting"] = {"price": 230, "seen": NOW.isoformat()}
        searched_with_grid(state, item(15), hours_ago=0.5, price=245, grid_at=220, stops=1)
        # the calendar falls $30 more: estimate $215, beating the record
        picks = planner.plan(c, state, budget=10, grid=grid({15: 190}, stops=1), now=NOW)
        reasons = {p.item: p.reason for p in picks}
        self.assertIn("record", reasons[item(15)])


class TestOverdue(unittest.TestCase):
    def test_nothing_starves(self):
        """A dull item searched long ago outranks a cheap one just searched."""
        c = cfg()
        state = State()
        prices = {offset: 400 + offset for offset in range(14, 24)}
        prices[14] = 300  # cheapest -> 1h interval
        for offset in range(14, 24):
            searched(state, item(offset), hours_ago=0.5)
        searched(state, item(23), hours_ago=30)  # dear -> 24h interval, 30h ago
        picks = planner.plan(c, state, budget=1, grid=grid(prices), now=NOW)
        self.assertEqual(picks[0].item, item(23))

    def test_never_searched_is_due_but_not_infinitely_overdue(self):
        """Otherwise a fresh state spends its first day on coverage alone."""
        c = cfg()
        state = State()
        for offset in range(14, 24):
            if offset != 18:
                searched(state, item(offset), hours_ago=0.1)
        # item 18 never searched; item 16 under the alert line, searched 1h ago
        searched(state, item(16), hours_ago=1)
        picks = planner.plan(c, state, budget=1, grid=grid({16: 240}), now=NOW)
        self.assertEqual(picks[0].item, item(16))

    def test_budget_is_respected(self):
        picks = planner.plan(cfg(), State(), budget=3, now=NOW)
        self.assertEqual(len(picks), 3)

    def test_falls_back_to_full_search_prices_without_a_calendar(self):
        c = cfg()
        state = State()
        searched(state, item(17), hours_ago=0.5, price=240)  # under the line
        for offset in range(14, 24):
            if offset != 17:
                searched(state, item(offset), hours_ago=0.5, price=420)
        picks = planner.plan(c, state, budget=1, grid=None, now=NOW)
        self.assertEqual(picks[0].item, item(17))

    def test_last_success_stands_in_for_a_missing_search_time(self):
        """State written before `checked` existed still has `latest.seen`."""
        c = cfg()
        state = State()
        stamp = (NOW - dt.timedelta(hours=10)).isoformat()
        state.latest[planner.item_key(item(15)) + "|0"] = {"price": 420, "seen": stamp}
        self.assertAlmostEqual(planner._age_hours(state, item(15), NOW), 10, places=3)


class TestGridView(unittest.TestCase):
    def test_stale_stored_scans_are_ignored(self):
        state = State()
        old = (NOW - dt.timedelta(hours=planner.GRID_MAX_AGE_HOURS + 1)).isoformat()
        state.grid["LAX|5|0"] = {"seen": old, "prices": {day(15): 100}}
        self.assertEqual(planner.grid_view(state, None, NOW), {})

    def test_fresh_scans_override_stored_ones(self):
        state = State()
        state.grid["LAX|5|0"] = {"seen": NOW.isoformat(), "prices": {day(15): 100}}
        view = planner.grid_view(state, {"LAX|5|0": {day(15): 90}}, NOW)
        self.assertEqual(view["LAX|5|0"][day(15)], 90)


class TestPartition(unittest.TestCase):
    def test_round_robin_spreads_the_urgent_ones(self):
        picks = planner.plan(cfg(), State(), budget=8, now=NOW)
        shards = planner.partition(picks, 4)
        self.assertEqual([len(s) for s in shards], [2, 2, 2, 2])
        self.assertEqual(shards[0][0], picks[0].item)
        self.assertEqual(shards[1][0], picks[1].item)

    def test_more_shards_than_items(self):
        picks = planner.plan(cfg(), State(), budget=2, now=NOW)
        shards = planner.partition(picks, 4)
        self.assertEqual(sum(len(s) for s in shards), 2)


if __name__ == "__main__":
    unittest.main()
