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
        url="https://flights.test/x",
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


def fare_rows(state, limit=10):
    """Just the fare lines, without the section headers."""
    return [
        line
        for line in cheapest_lines(state, make_config(), limit).splitlines()
        if line.startswith("**[") or line.startswith("**$")
    ]


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

    def test_selects_by_price_but_lists_by_departure(self):
        """Cheapest five, read in trip order."""
        state = stocked([
            offer(300, "2026-12-01", flight_no="AA 1"),
            offer(280, "2026-11-01", flight_no="AA 2"),
            offer(260, "2026-10-01", flight_no="AA 3"),
            offer(900, "2026-09-25", flight_no="AA 4"),
        ])
        rows = fare_rows(state, limit=3)
        self.assertFalse(any("$900" in r for r in rows))  # selection is by price
        self.assertIn("Oct 1", rows[0])
        self.assertIn("Nov 1", rows[1])
        self.assertIn("Dec 1", rows[2])

    def test_price_is_the_only_link(self):
        body = cheapest_lines(
            stocked([offer(278, flight_no="WN 2536", dep_airport="LGA")]),
            make_config(),
        )
        self.assertEqual(body.count("]("), 1)
        self.assertNotIn("southwest.com", body)

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

    def test_reads_in_decision_order(self):
        line = fare_rows(stocked([offer(338)]))[0]
        self.assertTrue(line.startswith("**[$338]"))
        for earlier, later in (
            ("$338", "Wed Oct 14"),
            ("Wed Oct 14", "JFK"),
            ("JFK", "JetBlue"),
        ):
            self.assertLess(line.index(earlier), line.index(later))

    def test_price_is_a_link(self):
        """A flight number is a poor handle on a fare months out; the link isn't."""
        line = fare_rows(stocked([offer(338, url="https://flights.test/x")]))[0]
        self.assertTrue(line.startswith("**[$338](https://flights.test/x)**"))

    def test_price_without_a_url_still_renders(self):
        line = fare_rows(stocked([offer(338, url=None)]))[0]
        self.assertTrue(line.startswith("**$338**"))

    def test_links_dropped_rather_than_truncated_when_too_long(self):
        """Better a linkless row than a row cut through the middle of a URL."""
        offers = [
            offer(300 + i, "2026-10-%02d" % (10 + i), url="https://x.test/" + "u" * 400)
            for i in range(12)
        ]
        body = cheapest_lines(stocked(offers), make_config(), limit=12)
        self.assertNotIn("](", body)
        self.assertIn("$300", body)
        self.assertLess(len(body), 4096)

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
        rows = fare_rows(
            stocked([offer(338, "2026-10-14"), offer(400, "2026-11-02")])
        )
        self.assertEqual(len(rows), 2)

    def test_layovers_are_labelled(self):
        self.assertIn("1 stop", rendered(stocked([offer(200, stops=1)])))
        self.assertIn("nonstop", rendered(stocked([offer(200, stops=0)])))

    def test_shows_the_airline_not_the_flight_number(self):
        body = rendered(stocked([offer(338, flight_no="AA 171", airlines=["American"])]))
        self.assertIn("American", body)
        self.assertNotIn("AA 171", body)

    def test_missing_airline_does_not_break_the_row(self):
        self.assertIn("$338", rendered(stocked([offer(338, airlines=[])])))

    def test_missing_details_render_placeholders(self):
        body = rendered(
            stocked([offer(300, dep_airport=None, arr_airport=None, dep_time=None,
                           airlines=[])])
        )
        self.assertIn("???", body)
        self.assertIn("$300", body)


class TestSections(unittest.TestCase):
    """Connecting fares are reliably cheaper here, so one ranked list would
    hand them every slot and the nonstops would never appear."""

    def mixed(self):
        return stocked([
            offer(230, "2026-10-01", stops=1, flight_no="WN 1"),
            offer(240, "2026-10-05", stops=1, flight_no="WN 2"),
            offer(250, "2026-10-09", stops=1, flight_no="WN 3"),
            offer(260, "2026-10-13", stops=1, flight_no="WN 4"),
            offer(357, "2026-11-01", stops=0, flight_no="AS 1"),
            offer(367, "2026-11-05", stops=0, flight_no="AS 2"),
        ])

    def test_both_sections_appear(self):
        body = cheapest_lines(self.mixed(), make_config(), limit=3)
        self.assertIn("**Nonstop**", body)
        self.assertIn("**One layover**", body)

    def test_nonstops_are_not_crowded_out(self):
        body = cheapest_lines(self.mixed(), make_config(), limit=3)
        self.assertIn("$357", body)
        self.assertIn("$367", body)

    def test_limit_applies_per_section(self):
        rows = fare_rows(self.mixed(), limit=3)
        self.assertEqual(len(rows), 5)  # 3 connecting + 2 nonstop available
        self.assertFalse(any("$260" in r for r in rows))  # 4th connecting dropped

    def test_empty_section_says_so(self):
        body = cheapest_lines(
            stocked([offer(230, stops=1)]), make_config(), limit=3
        )
        self.assertIn("none tracked yet", body)

    def test_nothing_at_all(self):
        self.assertIn("No fares tracked", cheapest_lines(State(), make_config()))


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
