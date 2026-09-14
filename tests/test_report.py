from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker.cli import _clock12, cheapest_lines
from flight_tracker.sources.base import Offer
from flight_tracker.state import State

NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)


def offer(price, out="2026-10-14", nights=3, **kw):
    ret = (dt.date.fromisoformat(out) + dt.timedelta(days=nights)).isoformat()
    base = dict(
        stops=0,
        airlines=["JetBlue"],
        dep_airport="JFK",
        arr_airport="LAX",
        dep_time="07:05",
        arr_time="10:40",
    )
    base.update(kw)
    return Offer(out_date=out, ret_date=ret, price=price, **base)


def rendered(state, limit=10):
    return cheapest_lines(state, make_config(), limit)


def stocked(offers) -> State:
    state = State()
    state.record(offers, make_config().history, now=NOW)
    return state


class TestClock(unittest.TestCase):
    def test_morning_and_afternoon(self):
        self.assertEqual(_clock12("07:05"), "7:05a")
        self.assertEqual(_clock12("17:30"), "5:30p")

    def test_midnight_and_noon_are_12(self):
        self.assertEqual(_clock12("00:15"), "12:15a")
        self.assertEqual(_clock12("12:00"), "12:00p")

    def test_missing_time_does_not_crash(self):
        for bad in (None, "", "nope", "aa:bb"):
            self.assertIn("--", _clock12(bad))


class TestCheapestReport(unittest.TestCase):
    def test_empty(self):
        self.assertIn("No fares tracked", cheapest_lines(State(), make_config()))

    def test_sorted_by_price_and_limited(self):
        state = stocked([
            offer(500, "2026-10-20"),
            offer(338, "2026-10-14"),
            offer(420, "2026-10-18"),
        ])
        body = rendered(state, limit=2)
        self.assertIn("$338", body)
        self.assertIn("$420", body)
        self.assertNotIn("$500", body)
        self.assertLess(body.index("$338"), body.index("$420"))

    def test_reads_in_decision_order(self):
        line = cheapest_lines(stocked([offer(338)]), make_config()).splitlines()[0]
        self.assertTrue(line.startswith("**$338**"))
        for earlier, later in (
            ("$338", "Wed Oct 14"),
            ("Wed Oct 14", "JFK"),
            ("JFK", "JetBlue"),
        ):
            self.assertLess(line.index(earlier), line.index(later))

    def test_no_code_fence(self):
        """Normal text reflows; a fixed-width table breaks mid-cell."""
        self.assertNotIn("```", cheapest_lines(stocked([offer(338)]), make_config()))

    def test_times_are_not_shown(self):
        body = cheapest_lines(
            stocked([offer(338, dep_time="07:05", arr_time="22:20")]), make_config()
        )
        self.assertNotIn("7:05a", body)
        self.assertNotIn("10:20p", body)

    def test_one_line_per_fare(self):
        body = cheapest_lines(
            stocked([offer(338, "2026-10-14"), offer(400, "2026-11-02")]),
            make_config(),
        )
        self.assertEqual(len([x for x in body.splitlines() if x.strip()]), 2)

    def test_layovers_are_labelled(self):
        self.assertIn("1 stop", rendered(stocked([offer(200, stops=1)])))
        self.assertIn("nonstop", rendered(stocked([offer(200, stops=0)])))

    def test_flight_number_replaces_airline_name(self):
        body = rendered(stocked([offer(338, flight_no="AA 171")]))
        self.assertIn("AA 171", body)
        self.assertNotIn("JetBlue", body)

    def test_falls_back_to_airline_when_number_missing(self):
        self.assertIn("JetBlue", rendered(stocked([offer(338)])))

    def test_missing_details_render_placeholders(self):
        body = rendered(
            stocked([offer(300, dep_airport=None, arr_airport=None, dep_time=None,
                           airlines=[])])
        )
        self.assertIn("???", body)
        self.assertIn("$300", body)


class TestDeduplication(unittest.TestCase):
    """One cheap outbound shows up once per return date; that's one option."""

    def test_same_flight_same_price_collapses(self):
        state = stocked([
            offer(278, "2026-10-21", nights=5, flight_no="WN 2536"),
            offer(278, "2026-10-21", nights=6, flight_no="WN 2536"),
            offer(278, "2026-10-21", nights=7, flight_no="WN 2536"),
        ])
        table = cheapest_lines(state, make_config(), limit=5)
        rows = [r for r in table.splitlines() if "$278" in r]
        self.assertEqual(len(rows), 1)
        self.assertIn("+2 more dates", rows[0])

    def test_singular_wording(self):
        state = stocked([
            offer(278, "2026-10-21", nights=5, flight_no="WN 2536"),
            offer(278, "2026-10-21", nights=6, flight_no="WN 2536"),
        ])
        self.assertIn("+1 more date", cheapest_lines(state, make_config()))

    def test_shortest_trip_represents_the_group(self):
        state = stocked([
            offer(278, "2026-10-21", nights=7, flight_no="WN 2536"),
            offer(278, "2026-10-21", nights=5, flight_no="WN 2536"),
        ])
        row = [r for r in cheapest_lines(state, make_config()).splitlines()
               if "$278" in r][0]
        self.assertIn("Mon Oct 26", row)  # the 5-night return

    def test_different_flights_stay_separate(self):
        state = stocked([
            offer(278, "2026-10-21", nights=5, flight_no="WN 2536"),
            offer(278, "2026-11-04", nights=5, flight_no="WN 264"),
        ])
        table = cheapest_lines(state, make_config(), limit=5)
        self.assertEqual(len([r for r in table.splitlines() if "$278" in r]), 2)

    def test_different_prices_stay_separate(self):
        state = stocked([
            offer(278, "2026-10-21", nights=5, flight_no="WN 2536"),
            offer(315, "2026-10-21", nights=6, flight_no="WN 2536"),
        ])
        table = cheapest_lines(state, make_config(), limit=5)
        self.assertIn("$278", table)
        self.assertIn("$315", table)

    def test_limit_counts_distinct_fares_not_rows(self):
        """Dedupe must free up slots, not just hide rows."""
        offers = []
        for i in range(3):
            offers += [offer(278, "2026-10-21", nights=n, flight_no="WN 2536")
                       for n in (5, 6, 7)]
        offers.append(offer(300, "2026-11-04", nights=5, flight_no="WN 999"))
        table = cheapest_lines(stocked(offers), make_config(), limit=2)
        self.assertIn("$300", table)


class TestLatestSnapshot(unittest.TestCase):
    def test_refreshes_even_when_the_price_point_is_skipped(self):
        """Redundant prices aren't logged, but the report must not go stale."""
        cfg = make_config().history
        state = State()
        state.record([offer(338, dep_time="07:05")], cfg, now=NOW)
        state.record(
            [offer(338, dep_time="09:30")], cfg, now=NOW + dt.timedelta(hours=1)
        )
        self.assertEqual(len(state.observations["2026-10-14|2026-10-17|LAX|0"]), 1)
        self.assertEqual(state.latest["2026-10-14|2026-10-17|LAX|0"]["dep_time"], "09:30")

    def test_trim_drops_departed_flights(self):
        state = stocked([offer(338, "2026-09-01")])
        state.trim(30, now=NOW)
        self.assertEqual(state.latest, {})


if __name__ == "__main__":
    unittest.main()
