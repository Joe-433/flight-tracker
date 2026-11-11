from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace as NS

from helpers import make_config

from flight_tracker import holiday
from flight_tracker.config import Holiday

UTC = dt.timezone.utc


def cfg(**kw):
    c = make_config(route={"destinations": ["LAX", "BUR"]}, source={"jitter_seconds": [0, 0]})
    c.holiday = Holiday(
        until="2026-11-18T00:00:00-08:00",
        outbound_dates=["2026-12-18", "2026-12-20"],
        preferred_outbound=["2026-12-19", "2026-12-20"],
        return_dates=["2027-01-02", "2027-01-03"],
        preferred_return=["2027-01-03", "2027-01-03"],
        layover_airports=["LAS", "DEN"],
        **kw,
    )
    return c


def seg(frm, to, dep, arr):
    return {"from": frm, "to": to, "dep": dep, "arr": arr}


def leg(direction, day, airport, price, layover=False, airline="JetBlue"):
    if layover:
        segs = [seg("JFK", "LAS", day + "T07:00", day + "T10:00"),
                seg("LAS", airport, day + "T19:00", day + "T20:30")]
        lay = {"airport": "LAS", "minutes": 540, "start": day + "T10:00", "end": day + "T19:00"}
    else:
        segs = [seg("JFK", airport, day + "T08:00", day + "T11:20")]
        lay = None
    if direction == "back":
        segs = [dict(s, **{"from": s["to"], "to": s["from"]}) for s in reversed(segs)]
    return {"direction": direction, "date": day, "airport": airport, "price": float(price),
            "airlines": [airline], "flight_no": None, "segments": segs, "layover": lay,
            "url": "https://g.test/%s/%s" % (direction, day)}


def doc_with(*legs):
    doc = {"legs": {}}
    for item in legs:
        doc["legs"].setdefault(holiday.leg_key(item["direction"], item["date"], item["airport"]), []).append(item)
    return doc


class LayoverRules(unittest.TestCase):
    def check(self, *segments):
        return holiday.layover_ok(list(segments), cfg())

    def test_nonstop_ok(self):
        self.assertTrue(self.check(seg("JFK", "LAX", "2026-12-19T08:00", "2026-12-19T11:00"))[0])

    def test_long_daytime_major_city_ok(self):
        ok, info, _ = self.check(seg("JFK", "LAS", "2026-12-19T07:59", "2026-12-19T11:09"),
                                 seg("LAS", "LAX", "2026-12-19T20:12", "2026-12-19T21:38"))
        self.assertTrue(ok)
        self.assertEqual(info["minutes"], 9 * 60 + 3)

    def test_short_layover_rejected(self):
        self.assertFalse(self.check(seg("LGA", "DEN", "2026-12-19T16:59", "2026-12-19T19:41"),
                                    seg("DEN", "LAX", "2026-12-19T21:15", "2026-12-19T22:56"))[0])

    def test_overnight_rejected(self):
        ok, _, reason = self.check(seg("JFK", "DEN", "2026-12-19T18:00", "2026-12-19T21:00"),
                                   seg("DEN", "LAX", "2026-12-20T07:00", "2026-12-20T08:30"))
        self.assertFalse(ok)
        self.assertIn("overnight", reason)

    def test_small_hours_rejected_even_same_day(self):
        self.assertFalse(self.check(seg("JFK", "LAS", "2026-12-18T22:00", "2026-12-19T01:00"),
                                    seg("LAS", "LAX", "2026-12-19T10:00", "2026-12-19T11:00"))[0])

    def test_unlisted_city_rejected(self):
        self.assertFalse(self.check(seg("JFK", "BNA", "2026-12-19T07:00", "2026-12-19T09:00"),
                                    seg("BNA", "LAX", "2026-12-19T18:00", "2026-12-19T20:00"))[0])

    def test_two_stops_rejected(self):
        self.assertFalse(self.check(seg("JFK", "LAS", "2026-12-19T07:00", "2026-12-19T09:00"),
                                    seg("LAS", "DEN", "2026-12-19T18:00", "2026-12-19T19:00"),
                                    seg("DEN", "LAX", "2026-12-19T20:00", "2026-12-19T21:00"))[0])


class Combos(unittest.TestCase):
    def test_cheapest_pairs_one_per_date_pair(self):
        doc = doc_with(
            leg("out", "2026-12-18", "LAX", 200), leg("out", "2026-12-19", "BUR", 150),
            leg("back", "2027-01-02", "LAX", 180), leg("back", "2027-01-03", "BUR", 220),
        )
        found = holiday.combos(doc, cfg())
        self.assertEqual(len(found), 4)
        self.assertEqual((found[0]["out_date"], found[0]["ret_date"], found[0]["total"]),
                         ("2026-12-19", "2027-01-02", 330.0))

    def test_layover_needs_to_save_the_bonus(self):
        c = cfg(nonstop_bonus_usd=25)
        doc = doc_with(leg("out", "2026-12-18", "LAX", 300), leg("out", "2026-12-18", "BUR", 290, layover=True),
                       leg("back", "2027-01-02", "LAX", 100))
        best = holiday.combos(doc, c)[0]
        self.assertIsNone(best["out"]["layover"])  # $10 saving isn't enough
        doc = doc_with(leg("out", "2026-12-18", "LAX", 300), leg("out", "2026-12-18", "BUR", 250, layover=True),
                       leg("back", "2027-01-02", "LAX", 100))
        self.assertIsNotNone(holiday.combos(doc, c)[0]["out"]["layover"])

    def test_roundtrip_replaces_when_cheaper(self):
        doc = doc_with(leg("out", "2026-12-18", "LAX", 300), leg("back", "2027-01-02", "LAX", 300))
        doc["roundtrips"] = {"2026-12-18|2027-01-02": {
            "out_date": "2026-12-18", "ret_date": "2027-01-02", "airport": "LAX", "price": 450.0,
            "airlines": ["Delta"], "segments": [seg("JFK", "LAX", "2026-12-18T08:00", "2026-12-18T11:00")],
            "url": "u"}}
        best = holiday.combos(doc, cfg())[0]
        self.assertEqual(best["kind"], "roundtrip")
        self.assertEqual(best["total"], 450.0)

    def test_preferred_and_nonstop_only(self):
        doc = doc_with(leg("out", "2026-12-18", "LAX", 100, layover=True), leg("out", "2026-12-20", "LAX", 300),
                       leg("back", "2027-01-02", "LAX", 100), leg("back", "2027-01-03", "LAX", 200))
        pref = holiday.combos(doc, cfg(), preferred_only=True)
        self.assertEqual({(c["out_date"], c["ret_date"]) for c in pref}, {("2026-12-20", "2027-01-03")})
        self.assertTrue(all(c["layovers"] == 0 for c in holiday.combos(doc, cfg(), nonstop_only=True)))

    def test_report_renders(self):
        doc = doc_with(leg("out", "2026-12-18", "LAX", 100, layover=True), leg("out", "2026-12-20", "LAX", 300),
                       leg("back", "2027-01-02", "LAX", 100), leg("back", "2027-01-03", "LAX", 200))
        doc["last_scan"] = "2026-11-11T14:00:00+00:00"
        doc["reports"] = [{"best": 230.0}]
        title, body, footer, top = holiday.report(doc, cfg(combos=2), dt.datetime(2026, 11, 11, 14, 30, tzinfo=UTC))
        self.assertEqual(len(top), 2)
        self.assertIn("**1. $200 total**", body)
        self.assertIn("9h in Las Vegas", body)
        self.assertIn("no layovers at all", body)
        self.assertIn("preferred days** (out Dec 19\u201320, back Jan 3)", body)
        self.assertIn("$30 cheaper", body)
        self.assertIn("resumes Wed Nov 18", footer)

    def test_empty_report(self):
        _, body, _, top = holiday.report({}, cfg(), dt.datetime(2026, 11, 11, tzinfo=UTC))
        self.assertEqual(top, [])
        self.assertIn("No complete trip", body)


class Timing(unittest.TestCase):
    def test_active_until(self):
        c = cfg()
        self.assertTrue(holiday.active(c, dt.datetime(2026, 11, 17, 12, tzinfo=UTC)))
        self.assertFalse(holiday.active(c, dt.datetime(2026, 11, 18, 8, 0, tzinfo=UTC)))
        c.holiday.until = None
        self.assertFalse(holiday.active(c))

    def test_daily_report_due_once(self):
        c = cfg()
        before = dt.datetime(2026, 11, 12, 13, 0, tzinfo=UTC)  # 5am PT
        after = dt.datetime(2026, 11, 12, 14, 30, tzinfo=UTC)  # 6:30am PT
        doc = {"last_report": "2026-11-11T14:05:00+00:00"}
        self.assertFalse(holiday.report_due(doc, c, before))
        self.assertTrue(holiday.report_due(doc, c, after))
        doc["last_report"] = after.isoformat()
        self.assertFalse(holiday.report_due(doc, c, after + dt.timedelta(hours=5)))

    def test_plan_skips_when_nothing_due(self):
        c = cfg()
        now = dt.datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
        doc = {"last_report": "2026-11-12T14:05:00+00:00", "last_scan": "2026-11-12T15:00:00+00:00"}
        self.assertFalse(holiday.make_plan(c, doc, now)["scan"])
        doc["last_scan"] = "2026-11-12T11:00:00+00:00"
        plan = holiday.make_plan(c, doc, now)
        self.assertTrue(plan["scan"])
        self.assertEqual(sum(len(s) for s in plan["shards"]), (3 + 2) * 2)


class Sweep(unittest.TestCase):
    def test_merge_keeps_unsearched_and_clears_searched(self):
        now = dt.datetime(2026, 11, 12, tzinfo=UTC)
        doc = doc_with(leg("out", "2026-12-18", "LAX", 300), leg("out", "2026-12-19", "LAX", 300))
        result = {"legs": [], "searched": [holiday.leg_key("out", "2026-12-18", "LAX")]}
        self.assertEqual(holiday.merge_legs(doc, [result], now), (1, 0))
        self.assertEqual(doc["legs"]["out|2026-12-18|LAX"], [])
        self.assertEqual(len(doc["legs"]["out|2026-12-19|LAX"]), 1)

    def test_source_filters_ewr_and_bad_layovers(self):
        def moment(day, hhmm):
            y, m, d = (int(x) for x in day.split("-"))
            return NS(date=(y, m, d), time=(int(hhmm[:2]), int(hhmm[3:])))

        def flight(frm, to, day, dep, arr):
            return NS(from_airport=NS(code=frm), to_airport=NS(code=to),
                      departure=moment(day, dep), arrival=moment(day, arr))

        day = "2026-12-19"
        results = [
            NS(price=150, airlines=["United"], flights=[flight("EWR", "LAX", day, "08:00", "11:00")]),
            NS(price=160, airlines=["United"], flights=[flight("LGA", "DEN", day, "08:00", "10:00"),
                                                       flight("DEN", "LAX", day, "11:00", "12:30")]),
            NS(price=170, airlines=["American"], flights=[flight("JFK", "LAS", day, "08:00", "11:00"),
                                                         flight("LAS", "LAX", day, "20:00", "21:30")]),
            NS(price=400, airlines=["JetBlue"], flights=[flight("JFK", "LAX", day, "08:00", "11:20")]),
        ]
        src = holiday.OneWaySource.__new__(holiday.OneWaySource)
        src.cfg = cfg()
        src.errors = []
        src._query = lambda *a, **k: NS(url=lambda: "u")
        src._results = lambda q, label: [(r, None) for r in results]
        legs = src.leg("out", day, "LAX")
        self.assertEqual([(l["price"], bool(l["layover"])) for l in legs], [(400.0, False), (170.0, True)])


class ReviewFixes(unittest.TestCase):
    def test_failed_search_keeps_old_prices(self):
        class Flaky:
            errors = []
            def leg(self, direction, day, airport):
                if airport == "BUR":
                    self.errors.append("blocked")
                    return []
                return [leg(direction, day, airport, 100)]
        plan = {"shards": [[["out", "2026-12-18", "LAX"], ["out", "2026-12-18", "BUR"]]]}
        result = holiday.run_shard(cfg(), plan, 0, source=Flaky())
        self.assertEqual(result["failed"], ["out|2026-12-18|BUR"])
        doc = doc_with(leg("out", "2026-12-18", "BUR", 90))
        holiday.merge_legs(doc, [result], dt.datetime(2026, 11, 12, tzinfo=UTC))
        self.assertEqual(doc["legs"]["out|2026-12-18|BUR"][0]["price"], 90.0)

    def test_pending_report_rescans_at_most_hourly(self):
        now = dt.datetime(2026, 11, 12, 15, 0, tzinfo=UTC)  # 7am PT, report due
        doc = {"last_report": "2026-11-11T14:05:00+00:00", "last_scan": "2026-11-12T14:40:00+00:00"}
        self.assertFalse(holiday.make_plan(cfg(), doc, now)["scan"])
        doc["last_scan"] = "2026-11-12T13:30:00+00:00"
        self.assertTrue(holiday.make_plan(cfg(), doc, now)["scan"])

    def test_roundtrip_must_be_cheaper_in_dollars(self):
        doc = doc_with(leg("out", "2026-12-18", "LAX", 200, layover=True), leg("back", "2027-01-02", "LAX", 200))
        doc["roundtrips"] = {"2026-12-18|2027-01-02": {
            "out_date": "2026-12-18", "ret_date": "2027-01-02", "airport": "LAX", "price": 415.0,
            "airlines": ["Delta"], "segments": [seg("JFK", "LAX", "2026-12-18T08:00", "2026-12-18T11:00")],
            "url": "u"}}
        self.assertEqual(holiday.combos(doc, cfg())[0]["kind"], "oneways")

    def test_deadman_ignores_the_pause(self):
        from flight_tracker import deadman
        from flight_tracker.state import State
        c = cfg()
        state = State()
        state.last_data = "2026-11-10T20:00:00+00:00"
        just_after = dt.datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # 1h after resume
        self.assertFalse(deadman.update(state, c, got_data=False, now=just_after).down)
        later = dt.datetime(2026, 11, 18, 16, 0, tzinfo=UTC)
        self.assertTrue(deadman.update(state, c, got_data=False, now=later).down)


if __name__ == "__main__":
    unittest.main()
