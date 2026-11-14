from __future__ import annotations

import datetime as dt
import unittest

from helpers import make_config

from flight_tracker import trip
from flight_tracker.config import Pause, Trip

UTC = dt.timezone.utc


def cfg(**kw):
    c = make_config(source={"jitter_seconds": [0, 0]})
    base = dict(
        until="2027-02-01T00:00:00-05:00", name="Guatemala", home="/m/02_286", away="GUA",
        outbound_dates=["2027-03-04", "2027-03-05"], arrive_by="2027-03-05T23:30",
        return_dates=["2027-03-08", "2027-03-10"], leave_after="2027-03-08T16:00",
        abroad_airports=["SAL", "YUL"],
    )
    base.update(kw)
    c.trip = Trip(**base)
    c.pause = Pause(regular_until="2026-11-18T00:00:00-08:00")
    return c


def seg(frm, to, dep, arr):
    return {"from": frm, "to": to, "dep": dep, "arr": arr}


def leg(direction, day, price, via=None, land="10:00", leave="18:30", airline="United", dep="07:00", arr="21:00"):
    a, b = ("JFK", "GUA") if direction == "out" else ("GUA", "JFK")
    if via:
        segs = [seg(a, via, "%sT%s" % (day, dep), "%sT%s" % (day, land)),
                seg(via, b, "%sT%s" % (day, leave), "%sT%s" % (day, arr))]
    else:
        segs = [seg(a, b, "%sT%s" % (day, dep), "%sT%s" % (day, arr))]
    return {"direction": direction, "date": day, "kind": "?", "price": float(price),
            "airlines": [airline], "flight_no": None, "segments": segs, "layover": None,
            "url": "https://g.test/x"}


def doc_with(*legs):
    doc = {"searches": {}}
    for item in legs:
        doc["searches"].setdefault(trip.item_key(item["direction"], item["date"], "any"), []).append(item)
    return doc


class Classify(unittest.TestCase):
    def kind(self, *segments):
        return trip.classify(list(segments), cfg())

    def test_nonstop(self):
        self.assertEqual(self.kind(seg("EWR", "GUA", "2027-03-05T18:36", "2027-03-05T22:48"))[0], "nonstop")

    def test_long_daytime_layover_is_adventure_and_abroad(self):
        kind, lay = self.kind(seg("JFK", "SAL", "2027-03-05T05:50", "2027-03-05T09:55"),
                              seg("SAL", "GUA", "2027-03-05T17:40", "2027-03-05T18:25"))
        self.assertEqual(kind, "adventure")
        self.assertTrue(lay["abroad"])
        self.assertEqual(lay["minutes"], 7 * 60 + 45)

    def test_short_or_overnight_or_predawn_is_regular(self):
        short = self.kind(seg("JFK", "MIA", "2027-03-05T07:51", "2027-03-05T11:11"),
                          seg("MIA", "GUA", "2027-03-05T13:17", "2027-03-05T15:12"))
        night = self.kind(seg("GUA", "SDQ", "2027-03-09T18:15", "2027-03-09T22:00"),
                          seg("SDQ", "EWR", "2027-03-10T06:00", "2027-03-10T09:30"))
        dawn = self.kind(seg("GUA", "DFW", "2027-03-09T01:22", "2027-03-09T05:03"),
                         seg("DFW", "EWR", "2027-03-09T12:05", "2027-03-09T16:32"))
        self.assertEqual([short[0], night[0], dawn[0]], ["regular"] * 3)
        self.assertTrue(night[1]["overnight"])

    def test_airport_change_and_two_stops_unusable(self):
        self.assertIsNone(self.kind(seg("EWR", "DCA", "2027-03-04T09:00", "2027-03-04T10:29"),
                                    seg("IAD", "GUA", "2027-03-04T17:43", "2027-03-04T21:25"))[0])
        self.assertIsNone(self.kind(seg("A", "B", "2027-03-04T09:00", "2027-03-04T10:00"),
                                    seg("B", "C", "2027-03-04T11:00", "2027-03-04T12:00"),
                                    seg("C", "D", "2027-03-04T13:00", "2027-03-04T14:00"))[0])

    def test_fixed_points(self):
        c = cfg()
        late = [seg("LAX", "GUA", "2027-03-05T22:56", "2027-03-06T05:40")]
        self.assertFalse(trip.fits("out", late, c))
        self.assertTrue(trip.fits("out", [seg("EWR", "GUA", "2027-03-05T18:36", "2027-03-05T22:48")], c))
        self.assertFalse(trip.fits("back", [seg("GUA", "EWR", "2027-03-08T09:30", "2027-03-08T22:30")], c))
        self.assertTrue(trip.fits("back", [seg("GUA", "EWR", "2027-03-08T23:57", "2027-03-09T05:30")], c))
        self.assertFalse(trip.fits("back", [seg("GUA", "EWR", "2027-03-08T18:50", "2027-03-08T23:59")], cfg(leave_after="2027-03-08T21:00")))


class Combos(unittest.TestCase):
    def setUp(self):
        self.doc = doc_with(
            leg("out", "2027-03-04", 364), leg("out", "2027-03-05", 444),
            leg("out", "2027-03-04", 280, via="MIA", land="11:11", leave="13:17"),
            leg("out", "2027-03-04", 293, via="YUL", land="08:31", leave="17:25", airline="Air Canada"),
            leg("out", "2027-03-05", 260, via="MIA", land="10:50", leave="18:50", airline="American"),
            leg("back", "2027-03-09", 337, dep="23:57", arr="23:59"),
            leg("back", "2027-03-10", 329, via="FLL", land="17:00", leave="20:00", dep="13:14", arr="23:49", airline="JetBlue"),
        )

    def test_nonstop_pairs(self):
        found = trip.nonstop_combos(self.doc, cfg())
        self.assertEqual([(c["out_date"], c["ret_date"], c["total"]) for c in found],
                         [("2027-03-04", "2027-03-09", 701.0), ("2027-03-05", "2027-03-09", 781.0)])

    def test_baseline_skips_adventure_legs(self):
        best = trip.any_combos(self.doc, cfg())[0]
        self.assertEqual(best["total"], 280.0 + 329.0)
        self.assertEqual(best["out"]["kind"], "regular")

    def test_adventures_one_per_city_and_abroad_gets_a_slot(self):
        found = trip.adventure_combos(self.doc, cfg(), 3)
        self.assertEqual([c["out"]["layover"]["airport"] for c in found], ["MIA", "YUL"])
        self.assertEqual(found[0]["total"], 260.0 + 329.0)
        only_one = trip.adventure_combos(self.doc, cfg(), 1)
        self.assertEqual(only_one[0]["out"]["layover"]["airport"], "YUL")  # abroad takes the last slot

    def test_roundtrip_only_when_cheaper(self):
        self.doc["roundtrips"] = {"2027-03-04|2027-03-09|0": {
            "out_date": "2027-03-04", "ret_date": "2027-03-09", "price": 720.0, "airlines": ["United"],
            "segments": [seg("EWR", "GUA", "2027-03-04T18:36", "2027-03-04T22:48")], "url": "u"}}
        self.assertEqual(trip.nonstop_combos(self.doc, cfg())[0]["kind"], "oneways")
        self.doc["roundtrips"]["2027-03-04|2027-03-09|0"]["price"] = 650.0
        best = trip.nonstop_combos(self.doc, cfg())[0]
        self.assertEqual((best["kind"], best["total"]), ("roundtrip", 650.0))
        targets = trip.roundtrip_targets(self.doc, cfg(roundtrip_checks=6))
        self.assertEqual(len(targets), 6 * 2)
        self.assertEqual(targets[0], ("2027-03-04", "2027-03-10", 0))

    def test_connecting_roundtrip_beats_one_ways_in_cheapest(self):
        self.doc["roundtrips"] = {"2027-03-04|2027-03-09|1": {
            "out_date": "2027-03-04", "ret_date": "2027-03-09", "price": 514.0, "max_stops": 1,
            "kind": "regular", "layover": None, "airlines": ["American"],
            "segments": [seg("EWR", "DFW", "2027-03-04T06:00", "2027-03-04T09:16"),
                         seg("DFW", "GUA", "2027-03-04T10:40", "2027-03-04T14:01")], "url": "u"}}
        best = trip.any_combos(self.doc, cfg())[0]
        self.assertEqual((best["kind"], best["total"], best["oneways_total"]), ("roundtrip", 514.0, 280.0 + 337.0))
        messages = trip.report(self.doc, cfg(), dt.datetime(2026, 11, 14, tzinfo=UTC))
        self.assertIn("$514 round-trip ticket on American", messages[0][1])
        self.assertIn("$103 less than the cheapest two one-ways", messages[1][1])

    def test_report_format(self):
        self.doc["last_scan"] = "2026-11-12T14:00:00+00:00"
        self.doc["reports"] = [{"best": {"any": 650.0}}]
        self.doc["hints"] = {"UA|JFK|GUA|2027-03-04|364": {"suggestion": "Wait", "confidence": 66, "max_saving": "40%"}}
        messages = trip.report(self.doc, cfg(), dt.datetime(2026, 11, 12, 14, 30, tzinfo=UTC))
        first, second = messages[0][1], messages[1][1]
        self.assertIn("**1. $364 + $337 = $701** · Thu Mar 4 → Tue Mar 9, 5 nights", first)
        self.assertIn("AirHint: wait, 66% sure it drops (up to 40%)", first)
        self.assertIn("$41 cheaper (was $650)", first)
        self.assertIn("in Montreal, Canada (8:31am–5:25pm)", second)
        self.assertIn("cheapest ordinary trip", second)
        self.assertTrue(all(len(m[1]) <= 4096 for m in messages))

    def test_empty_report(self):
        messages = trip.report({}, cfg(), dt.datetime(2026, 11, 12, tzinfo=UTC))
        self.assertIn("No complete trip", messages[0][1])

    def test_drop_alert_against_what_was_last_told(self):
        c = cfg()
        self.assertEqual(trip.drop_alerts(self.doc, c), [])      # nothing told yet
        self.doc["told"] = {"nonstop": 730.0, "any": 615.0}
        fired = trip.drop_alerts(self.doc, c)
        self.assertEqual([(k, was) for k, _, was in fired], [("nonstop", 730.0)])
        self.assertEqual(trip.drop_alerts(self.doc, c), fired)   # still owed until delivered

    def test_stale_legs_dropped(self):
        self.doc["last_scan"] = "2026-11-14T00:00:00+00:00"
        for legs in self.doc["searches"].values():
            for item in legs:
                item["seen"] = "2026-11-12T00:00:00+00:00"
        self.assertEqual(trip.nonstop_combos(self.doc, cfg()), [])


class Timing(unittest.TestCase):
    def test_modes(self):
        c = cfg()
        self.assertTrue(trip.active(c, dt.datetime(2026, 11, 12, tzinfo=UTC)))
        self.assertFalse(trip.active(c, dt.datetime(2027, 2, 1, 6, tzinfo=UTC)))
        self.assertTrue(trip.regular_paused(c, dt.datetime(2026, 11, 17, tzinfo=UTC)))
        self.assertFalse(trip.regular_paused(c, dt.datetime(2026, 11, 18, 9, tzinfo=UTC)))
        c.trip = Trip()
        c.pause = Pause()
        self.assertFalse(trip.active(c))
        self.assertFalse(trip.regular_paused(c))

    def test_plan(self):
        c = cfg()
        now = dt.datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
        doc = {"last_report": "2026-11-13T14:05:00+00:00", "last_scan": "2026-11-13T15:00:00+00:00"}
        self.assertFalse(trip.make_plan(c, doc, now)["scan"])
        doc["last_scan"] = "2026-11-13T11:00:00+00:00"
        plan = trip.make_plan(c, doc, now)
        self.assertEqual(sum(len(s) for s in plan["shards"]), (2 + 3) * 4)
        # a pending report rescans at most hourly
        doc = {"last_report": "2026-11-12T14:05:00+00:00", "last_scan": "2026-11-13T17:40:00+00:00"}
        self.assertFalse(trip.make_plan(c, doc, now)["scan"])

    def test_daily_report_due_once(self):
        c = cfg()
        doc = {"last_report": "2026-11-12T14:05:00+00:00"}
        self.assertFalse(trip.report_due(doc, c, dt.datetime(2026, 11, 13, 13, 0, tzinfo=UTC)))
        self.assertTrue(trip.report_due(doc, c, dt.datetime(2026, 11, 13, 14, 30, tzinfo=UTC)))

    def test_deadman_ignores_the_pause(self):
        from flight_tracker import deadman
        from flight_tracker.state import State
        state = State()
        state.last_data = "2026-11-10T20:00:00+00:00"
        self.assertFalse(deadman.update(state, cfg(), got_data=False,
                                        now=dt.datetime(2026, 11, 18, 9, 0, tzinfo=UTC)).down)


class Sweep(unittest.TestCase):
    def test_failed_search_keeps_old_prices_empty_clears(self):
        class Flaky:
            errors = []
            def leg(self, direction, day, query_kind):
                if query_kind == "long":
                    self.errors.append("blocked")
                    return []
                if query_kind == "abroad":
                    self.errors.append("FlightsNotFound: none")
                    return []
                return [leg(direction, day, 100)]
        plan = {"shards": [[["out", "2027-03-04", q] for q in ("nonstop", "long", "abroad")]]}
        result = trip.run_shard(cfg(), plan, 0, source=Flaky())
        self.assertEqual(result["failed"], ["out|2027-03-04|long"])
        doc = {"searches": {"out|2027-03-04|long": [leg("out", "2027-03-04", 90)],
                            "out|2027-03-04|abroad": [leg("out", "2027-03-04", 80)]}}
        self.assertEqual(trip.merge(doc, [result], dt.datetime(2026, 11, 12, tzinfo=UTC)), (2, 1))
        self.assertEqual(doc["searches"]["out|2027-03-04|long"][0]["price"], 90.0)
        self.assertEqual(doc["searches"]["out|2027-03-04|abroad"][0]["price"], 80.0)  # empty keeps old

    def test_same_flight_from_two_searches_counts_once(self):
        doc = {"searches": {"out|2027-03-04|any": [leg("out", "2027-03-04", 364)],
                            "out|2027-03-04|nonstop": [leg("out", "2027-03-04", 360)]}}
        legs = trip.legs_for(doc, cfg(), "out", "2027-03-04")
        self.assertEqual([l["price"] for l in legs], [360.0])

    def test_airline_code(self):
        self.assertEqual(trip.airline_code({"flight_no": "AA 431 / 1824", "airlines": ["American"]}), "AA")
        self.assertEqual(trip.airline_code({"flight_no": None, "airlines": ["Air Canada"]}), "AC")
        self.assertIsNone(trip.airline_code({"flight_no": None, "airlines": ["Mystery Air"]}))


if __name__ == "__main__":
    unittest.main()
