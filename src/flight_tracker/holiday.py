"""Holiday mode: one-way legs on fixed dates, combined into the cheapest trips.

The regular sweep watches roundtrips of a fixed length. A holiday trip is the
opposite shape: the outbound and return each have their own date range, and
two one-way tickets on different airlines are often cheaper than one
roundtrip. So this searches each leg on its own --

    out   NY metro -> each LA airport, every outbound date
    back  each LA airport -> NY metro, every return date

-- keeps the cheapest acceptable itinerary per search, and pairs every
outbound date with every return date. A handful of nonstop roundtrip searches
then check whether one ticket beats two for the cheapest date pairs.

Acceptable means nonstop, or exactly one layover that is long enough to be a
day in a major city: at least `layover_min_hours`, in one of
`layover_airports`, and entirely inside daytime hours on one calendar day. A
short connection or a night in an airport doesn't qualify.

A run has three stages, like the regular sweep:

    plan    is a scan due? split the searches into shards
    sweep   one shard's one-way searches (own runner, own IP)
    apply   merge, roundtrip checks, rank, daily report, save

State lives in its own file (data/holiday.json), so the regular sweep's
state.json is untouched while this runs and picks up where it left off.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .state import hours_since, parse_ts, utcnow

DEFAULT_STATE = os.path.join("data", "holiday.json")

# (direction, date, LA airport). "out" flies NY -> airport, "back" airport -> NY.
Item = Tuple[str, str, str]


# ---------------------------------------------------------------------------
# mode and timing
# ---------------------------------------------------------------------------


def active(cfg: Config, now: Optional[dt.datetime] = None) -> bool:
    """Is holiday mode on right now? Off once `until` has passed."""
    until = parse_ts(cfg.holiday.until)
    if until is None:
        return False
    now = now or utcnow()
    return now < until


def _days(span: List[str]) -> List[str]:
    if not span:
        return []
    first = dt.date.fromisoformat(span[0])
    last = dt.date.fromisoformat(span[-1])
    return [(first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _within(day: str, span: List[str]) -> bool:
    return bool(span) and span[0] <= day <= span[-1]


def report_slot(cfg: Config, now: dt.datetime) -> dt.datetime:
    """Today's report time if it has passed, else yesterday's. In UTC."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.report.timezone)
    local = now.astimezone(tz)
    slot = dt.datetime.combine(local.date(), dt.time(cfg.holiday.report_hour), tzinfo=tz)
    if slot > local:
        slot = dt.datetime.combine(
            local.date() - dt.timedelta(days=1), dt.time(cfg.holiday.report_hour), tzinfo=tz
        )
    return slot.astimezone(dt.timezone.utc)


def report_due(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> bool:
    sent = parse_ts(doc.get("last_report"))
    return sent is None or sent < report_slot(cfg, now)


def scan_due(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> bool:
    age = hours_since(doc.get("last_scan"), now)
    return age is None or age >= cfg.holiday.scan_every_hours


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def load(path: str = DEFAULT_STATE) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def save(doc: Dict[str, Any], path: str = DEFAULT_STATE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def items(cfg: Config) -> List[Item]:
    h = cfg.holiday
    out = [("out", day, airport) for day in _days(h.outbound_dates) for airport in cfg.route.destinations]
    back = [("back", day, airport) for day in _days(h.return_dates) for airport in cfg.route.destinations]
    return out + back


def make_plan(cfg: Config, doc: Dict[str, Any], now: Optional[dt.datetime] = None,
              force: bool = False) -> Dict[str, Any]:
    """Decide whether this run scans, and split the searches into shards.

    A scan runs every `scan_every_hours`, and always when a report is due, so
    the daily report is never built from prices hours old.
    """
    now = now or utcnow()
    due_report = report_due(doc, cfg, now)
    # A due report forces a fresh scan, but at most hourly: if Discord is down
    # the report retries without re-running 80 searches every 30 minutes.
    age = hours_since(doc.get("last_scan"), now)
    fresh_enough = age is not None and age < 1
    due = force or scan_due(doc, cfg, now) or (due_report and not fresh_enough)
    shards: List[List[Item]] = [[] for _ in range(max(1, cfg.holiday.shards))]
    if due:
        everything = items(cfg)
        # Deal round-robin so each shard gets a mix of dates and airports,
        # rather than one runner hammering a single route.
        for i, item in enumerate(everything):
            shards[i % len(shards)].append(item)
    return {
        "created": now.isoformat(),
        "scan": due,
        "report_due": due_report,
        "shards": shards,
    }


# ---------------------------------------------------------------------------
# layover rules
# ---------------------------------------------------------------------------


def _stamp(moment: Any) -> Optional[str]:
    """"YYYY-MM-DDTHH:MM" from a fast-flights SimpleDatetime, tolerantly."""
    try:
        y, m, d = (int(x) for x in moment.date)
        hh, mm = (int(x) for x in (list(moment.time) + [0, 0])[:2])
        return "%04d-%02d-%02dT%02d:%02d" % (y, m, d, hh, mm)
    except (AttributeError, TypeError, ValueError):
        return None


def layover_ok(segments: List[Dict[str, Any]], cfg: Config) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """(acceptable, layover details, reason) for one itinerary's segments.

    Nonstop is always fine. One stop is fine only as a daytime visit to a
    major city: at least `layover_min_hours`, at a listed airport, and the
    whole layover inside [day_start, day_end) on a single calendar day.
    """
    if len(segments) == 1:
        return True, None, "nonstop"
    if len(segments) != 2:
        return False, None, "%d stops" % (len(segments) - 1)

    first, second = segments
    airport = (first.get("to") or "").upper()
    if (second.get("from") or "").upper() != airport:
        return False, None, "changes airport mid-layover"
    land, leave = first.get("arr"), second.get("dep")
    if not land or not leave:
        return False, None, "layover times unknown"

    start = dt.datetime.fromisoformat(land)
    end = dt.datetime.fromisoformat(leave)
    minutes = int((end - start).total_seconds() // 60)
    info = {"airport": airport, "minutes": minutes, "start": land, "end": leave}

    h = cfg.holiday
    allowed = {a.upper() for a in h.layover_airports}
    if airport not in allowed:
        return False, info, "layover in %s, not a listed major city" % airport
    if minutes < h.layover_min_hours * 60:
        return False, info, "layover %dh%02d, under %gh" % (minutes // 60, minutes % 60, h.layover_min_hours)
    day_start, day_end = (list(h.layover_day_hours) + [6, 24])[:2]
    if start.date() != end.date():
        return False, info, "overnight layover"
    if start.hour < day_start or (end.hour + end.minute / 60.0) > day_end:
        return False, info, "layover runs into the night"
    return True, info, "long daytime layover"


# ---------------------------------------------------------------------------
# sweep (network)
# ---------------------------------------------------------------------------


class OneWaySource:
    """One-way and nonstop-roundtrip searches through fast-flights.

    Reuses the regular PairsSource's fetch/parse/flight-number plumbing; only
    the query shape and the acceptance rules differ.
    """

    def __init__(self, cfg: Config) -> None:
        from .sources.pairs import PairsSource

        self.cfg = cfg
        self.pairs = PairsSource(cfg)
        self.errors: List[str] = []

    def _query(self, trip: str, legs: List[Tuple[str, str, str]], max_stops: int) -> Any:
        s = self.cfg.search
        p = self.pairs
        return p._create_query(
            flights=[p._FlightQuery(date=d, from_airport=a, to_airport=b) for d, a, b in legs],
            trip=trip,
            seat=s.seat,
            passengers=p._Passengers(adults=s.adults),
            max_stops=max_stops,
            carry_on_bags=s.carry_on_bags,
            checked_bags=s.checked_bags,
            currency=s.currency,
            exclude_basic_economy=s.exclude_basic_economy,
        )

    def _results(self, query: Any, label: str) -> List[Tuple[Any, Optional[str]]]:
        """(itinerary, flight numbers) pairs, or [] with the error recorded."""
        p = self.pairs
        p.errors = []
        results = p._fetch(query, label, "", None)
        if p.errors:
            self.errors.extend(p.errors)
        if not results:
            return []
        numbers = p._numbers if len(p._numbers or []) == len(results) else []
        out = []
        for i, item in enumerate(results):
            flight_no = None
            if i < len(numbers):
                from .sources.pairs import _format_flight_no, carrier_matches

                candidate = _format_flight_no(numbers[i], len(getattr(item, "flights", []) or []))
                if carrier_matches(candidate, list(getattr(item, "airlines", []) or [])):
                    flight_no = candidate
            out.append((item, flight_no))
        return out

    def _excluded(self) -> set:
        return {a.strip().upper() for a in self.cfg.route.exclude_origins}

    def leg(self, direction: str, day: str, airport: str) -> List[Dict[str, Any]]:
        """Cheapest acceptable nonstop and cheapest acceptable layover, if any."""
        origin = self.cfg.route.origin
        frm, to = (origin, airport) if direction == "out" else (airport, origin)
        query = self._query("one-way", [(day, frm, to)], max_stops=1)
        url = query.url()
        label = "%s %s %s" % (direction, day, airport)

        best: Dict[str, Dict[str, Any]] = {}
        rejected = 0
        for item, flight_no in self._results(query, label):
            price = getattr(item, "price", None)
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            segments = []
            for f in list(getattr(item, "flights", []) or []):
                segments.append({
                    "from": getattr(getattr(f, "from_airport", None), "code", None),
                    "to": getattr(getattr(f, "to_airport", None), "code", None),
                    "dep": _stamp(getattr(f, "departure", None)),
                    "arr": _stamp(getattr(f, "arrival", None)),
                })
            if not segments:
                continue
            # The NY end is a city MID that includes EWR; drop it per
            # itinerary so the cheapest JFK/LGA option still wins.
            ny_end = segments[0]["from"] if direction == "out" else segments[-1]["to"]
            if (ny_end or "").upper() in self._excluded():
                continue
            ok, layover, _reason = layover_ok(segments, self.cfg)
            if not ok:
                rejected += 1
                continue
            kind = "nonstop" if len(segments) == 1 else "layover"
            if kind in best and best[kind]["price"] <= price:
                continue
            best[kind] = {
                "direction": direction,
                "date": day,
                "airport": airport,
                "price": float(price),
                "airlines": list(getattr(item, "airlines", []) or []),
                "flight_no": flight_no,
                "segments": segments,
                "layover": layover,
                "url": url,
            }
        return [best[k] for k in ("nonstop", "layover") if k in best]

    def roundtrip(self, out_date: str, ret_date: str, airport: str,
                  ny_airport: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Cheapest nonstop roundtrip for one date pair, LA airport and NY airport.

        The NY end is a named airport, not the city MID: the payload only
        details the outbound, so a MID return could quietly land at Newark.
        """
        origin = ny_airport or self.cfg.route.origin
        query = self._query(
            "round-trip", [(out_date, origin, airport), (ret_date, airport, origin)], max_stops=0
        )
        best = None
        for item, flight_no in self._results(query, "roundtrip %s %s %s" % (out_date, ret_date, airport)):
            price = getattr(item, "price", None)
            legs = list(getattr(item, "flights", []) or [])
            if not isinstance(price, (int, float)) or price <= 0 or len(legs) != 1:
                continue
            dep = getattr(getattr(legs[0], "from_airport", None), "code", None)
            if (dep or "").upper() in self._excluded():
                continue
            if best is None or price < best["price"]:
                best = {
                    "out_date": out_date,
                    "ret_date": ret_date,
                    "airport": airport,
                    "price": float(price),
                    "airlines": list(getattr(item, "airlines", []) or []),
                    "flight_no": flight_no,
                    "segments": [{
                        "from": dep,
                        "to": getattr(getattr(legs[0], "to_airport", None), "code", None),
                        "dep": _stamp(getattr(legs[0], "departure", None)),
                        "arr": _stamp(getattr(legs[0], "arrival", None)),
                    }],
                    "url": query.url(),
                }
        return best


def _pause(cfg: Config, first: bool) -> None:
    if first:
        return
    lo, hi = (list(cfg.source.jitter_seconds) + [0, 0])[:2]
    time.sleep(random.uniform(float(lo), float(hi)))


def run_shard(cfg: Config, plan: Dict[str, Any], shard: int, source: Any = None) -> Dict[str, Any]:
    shards = plan.get("shards") or []
    todo = shards[shard] if 0 <= shard < len(shards) else []
    source = source or OneWaySource(cfg)
    legs: List[Dict[str, Any]] = []
    searched: List[str] = []
    failed: List[str] = []
    for i, (direction, day, airport) in enumerate(todo):
        _pause(cfg, i == 0)
        errors_before = len(source.errors)
        found = source.leg(direction, day, airport)
        key = leg_key(direction, day, airport)
        # A search that errored (blocked, rate-limited, unparsable) says
        # nothing about the flights, so it must not wipe the last good ones.
        if len(source.errors) > errors_before and not found:
            failed.append(key)
            continue
        legs.extend(found)
        searched.append(key)
    return {"shard": shard, "legs": legs, "searched": searched, "failed": failed,
            "errors": list(source.errors)}


# ---------------------------------------------------------------------------
# combine and rank
# ---------------------------------------------------------------------------


def leg_key(direction: str, day: str, airport: str) -> str:
    return "%s|%s|%s" % (direction, day, airport)


def merge_legs(doc: Dict[str, Any], results: List[Dict[str, Any]], now: dt.datetime) -> Tuple[int, int]:
    """Fold shard results into doc["legs"]. Returns (searched, searches with a fare).

    A searched key is replaced wholesale -- including by nothing, if the search
    came back with no acceptable flight -- but a key whose shard died keeps its
    last known legs, stamped with when they were seen.
    """
    legs: Dict[str, List[Dict[str, Any]]] = dict(doc.get("legs") or {})
    searched = 0
    found = set()
    for result in results:
        fresh: Dict[str, List[Dict[str, Any]]] = {}
        for leg in result.get("legs") or []:
            leg = dict(leg, seen=now.isoformat())
            fresh.setdefault(leg_key(leg["direction"], leg["date"], leg["airport"]), []).append(leg)
        for key in result.get("searched") or []:
            searched += 1
            legs[key] = fresh.get(key, [])
            if fresh.get(key):
                found.add(key)
    doc["legs"] = legs
    return searched, len(found)


def _best_leg(doc: Dict[str, Any], direction: str, day: str, cfg: Config,
              nonstop_only: bool = False) -> Optional[Dict[str, Any]]:
    best = None
    for airport in cfg.route.destinations:
        for leg in (doc.get("legs") or {}).get(leg_key(direction, day, airport), []):
            if nonstop_only and leg.get("layover"):
                continue
            if best is None or _rank_price(leg, cfg) < _rank_price(best, cfg):
                best = leg
    return best


def _rank_price(leg: Dict[str, Any], cfg: Config) -> float:
    return float(leg["price"]) + (cfg.holiday.nonstop_bonus_usd if leg.get("layover") else 0.0)


def combos(doc: Dict[str, Any], cfg: Config, nonstop_only: bool = False,
           preferred_only: bool = False) -> List[Dict[str, Any]]:
    """Every outbound date x return date, cheapest legs each, best first.

    One combo per date pair: the list is a choice of travel dates, so three
    rows differing only by which equally-priced flight is on one leg would
    waste two of the three slots.
    """
    h = cfg.holiday
    out_days = _days(h.outbound_dates)
    back_days = _days(h.return_dates)
    if preferred_only:
        out_days = [d for d in out_days if _within(d, h.preferred_outbound)]
        back_days = [d for d in back_days if _within(d, h.preferred_return)]

    best_out = {d: _best_leg(doc, "out", d, cfg, nonstop_only) for d in out_days}
    best_back = {d: _best_leg(doc, "back", d, cfg, nonstop_only) for d in back_days}
    roundtrips = doc.get("roundtrips") or {}

    found = []
    for o in out_days:
        for b in back_days:
            out_leg, back_leg = best_out.get(o), best_back.get(b)
            if not out_leg or not back_leg:
                continue
            combo = {
                "kind": "oneways",
                "out_date": o,
                "ret_date": b,
                "out": out_leg,
                "back": back_leg,
                "total": float(out_leg["price"]) + float(back_leg["price"]),
                "rank": _rank_price(out_leg, cfg) + _rank_price(back_leg, cfg),
                "layovers": int(bool(out_leg.get("layover"))) + int(bool(back_leg.get("layover"))),
            }
            rt = roundtrips.get("%s|%s" % (o, b))
            if rt and float(rt["price"]) < combo["total"]:
                combo = {
                    "kind": "roundtrip",
                    "out_date": o,
                    "ret_date": b,
                    "roundtrip": rt,
                    "total": float(rt["price"]),
                    "rank": float(rt["price"]),
                    "layovers": 0,
                    "oneways_total": combo["total"],
                }
            found.append(combo)
    found.sort(key=lambda c: (c["rank"], c["layovers"], c["out_date"], c["ret_date"]))
    return found


def roundtrip_targets(doc: Dict[str, Any], cfg: Config) -> List[Tuple[str, str, str, str]]:
    """Date pairs worth a nonstop roundtrip search: the cheapest by one-ways.

    The airport is the one the cheapest nonstop outbound lands at, since a
    roundtrip ticket flies back from the same airport.
    """
    targets: List[Tuple[str, str, str, str]] = []
    for combo in combos(doc, cfg):
        if len(targets) >= cfg.holiday.roundtrip_checks:
            break
        out_leg = _best_leg(doc, "out", combo["out_date"], cfg, nonstop_only=True)
        if not out_leg:
            continue
        target = (combo["out_date"], combo["ret_date"], out_leg["airport"],
                  out_leg["segments"][0]["from"])
        if target not in targets:
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _money(value: float) -> str:
    return "$%.0f" % value


def _day(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%a %b %-d")


def _clock(stamp: Optional[str], ref_day: Optional[str] = None) -> str:
    if not stamp:
        return "?"
    moment = dt.datetime.fromisoformat(stamp)
    text = moment.strftime("%-I:%M%p").lower().replace(":00", "")
    if ref_day and moment.date().isoformat() > ref_day:
        text += " (+1)"
    return text


def _hours(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return "%dh" % h if not m else "%dh%02d" % (h, m)


CITY = {
    "ATL": "Atlanta", "BNA": "Nashville", "BOS": "Boston", "BWI": "Baltimore",
    "CLT": "Charlotte", "DAL": "Dallas", "DCA": "Washington DC", "DEN": "Denver",
    "DFW": "Dallas", "DTW": "Detroit", "FLL": "Fort Lauderdale", "HOU": "Houston",
    "IAD": "Washington DC", "IAH": "Houston", "LAS": "Las Vegas", "MCO": "Orlando",
    "MDW": "Chicago", "MIA": "Miami", "MSP": "Minneapolis", "MSY": "New Orleans",
    "ORD": "Chicago", "PDX": "Portland", "PHL": "Philadelphia", "PHX": "Phoenix",
    "SAN": "San Diego", "SEA": "Seattle", "SFO": "San Francisco", "SLC": "Salt Lake City",
    "AUS": "Austin", "OAK": "Oakland", "SJC": "San Jose", "STL": "St. Louis",
}


def _leg_line(leg: Dict[str, Any], heading: str) -> str:
    segs = leg["segments"]
    price = _money(leg["price"])
    if leg.get("url"):
        price = "[%s](%s)" % (price, leg["url"])
    airline = ", ".join(leg.get("airlines") or []) or "?"
    route = "%s %s → %s %s" % (
        segs[0]["from"], _clock(segs[0]["dep"]), segs[-1]["to"], _clock(segs[-1]["arr"], leg["date"]),
    )
    lay = leg.get("layover")
    if lay:
        stop = "1 stop: %s in %s (%s–%s)" % (
            _hours(lay["minutes"]), CITY.get(lay["airport"], lay["airport"]),
            _clock(lay["start"]), _clock(lay["end"]),
        )
    else:
        stop = "nonstop"
    flight = " %s" % leg["flight_no"] if leg.get("flight_no") else ""
    return "%s %s · %s · %s%s · %s · %s" % (
        heading, _day(leg["date"]), price, airline, flight, route, stop,
    )


def _combo_block(n: int, combo: Dict[str, Any]) -> str:
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        seg = rt["segments"][0]
        price = _money(rt["price"])
        if rt.get("url"):
            price = "[%s](%s)" % (price, rt["url"])
        airline = ", ".join(rt.get("airlines") or []) or "?"
        return (
            "**%d. %s total** — one nonstop roundtrip ticket (%s cheaper than two one-ways)\n"
            "Out %s · %s · %s · %s %s → %s %s · nonstop\n"
            "Back %s · nonstop from %s, pick the flight when booking"
            % (
                n, _money(rt["price"]), _money(combo["oneways_total"] - rt["price"]),
                _day(rt["out_date"]), price, airline,
                seg["from"], _clock(seg["dep"]), seg["to"], _clock(seg["arr"], rt["out_date"]),
                _day(rt["ret_date"]), rt["airport"],
            )
        )
    same = set(combo["out"].get("airlines") or []) == set(combo["back"].get("airlines") or [])
    tickets = "two one-way tickets" + ("" if same else ", different airlines")
    return "**%d. %s total** — %s\n%s\n%s" % (
        n, _money(combo["total"]), tickets,
        _leg_line(combo["out"], "Out"), _leg_line(combo["back"], "Back"),
    )


def _short(combo: Dict[str, Any]) -> str:
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        return "%s — roundtrip on %s, out %s, back %s" % (
            _money(combo["total"]), ", ".join(rt.get("airlines") or []) or "?",
            _day(combo["out_date"]), _day(combo["ret_date"]),
        )
    return "%s — out %s (%s, %s → %s), back %s (%s, %s → %s)" % (
        _money(combo["total"]),
        _day(combo["out_date"]), ", ".join(combo["out"].get("airlines") or []) or "?",
        combo["out"]["segments"][0]["from"], combo["out"]["segments"][-1]["to"],
        _day(combo["ret_date"]), ", ".join(combo["back"].get("airlines") or []) or "?",
        combo["back"]["segments"][0]["from"], combo["back"]["segments"][-1]["to"],
    )


def _ident(combo: Dict[str, Any]) -> Tuple[Any, ...]:
    """Same trip? Dates alone aren't enough: the nonstop-only option on the
    same dates as a layover option is a different trip worth showing."""
    if combo["kind"] == "roundtrip":
        return ("roundtrip", combo["out_date"], combo["ret_date"], combo["total"])
    return ("oneways", combo["out_date"], combo["ret_date"],
            combo["out"]["price"], combo["back"]["price"], combo["layovers"])


def _span(span: List[str]) -> str:
    if not span:
        return "?"
    a, b = dt.date.fromisoformat(span[0]), dt.date.fromisoformat(span[-1])
    if a == b:
        return a.strftime("%b %-d")
    if a.month == b.month:
        return "%s–%d" % (a.strftime("%b %-d"), b.day)
    return "%s – %s" % (a.strftime("%b %-d"), b.strftime("%b %-d"))


def report(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> Tuple[str, str, str, List[Dict[str, Any]]]:
    """(title, body, footer, top combos) for the daily report."""
    h = cfg.holiday
    top = combos(doc, cfg)[: h.combos]
    title = "%s flights · the %d cheapest ways to go" % (h.label, h.combos)

    lines: List[str] = []
    if not top:
        lines.append(
            "No complete trip found yet: the last search didn't find an acceptable "
            "flight for both directions. Nonstops and long daytime layovers only."
        )
    for i, combo in enumerate(top, 1):
        lines.append(_combo_block(i, combo))

    extras: List[str] = []
    shown = {_ident(c) for c in top}
    if top and any(c["layovers"] for c in top):
        nonstop = combos(doc, cfg, nonstop_only=True)
        if nonstop and _ident(nonstop[0]) not in shown:
            extras.append("**Cheapest with no layovers at all:** %s" % _short(nonstop[0]))
    preferred = combos(doc, cfg, preferred_only=True)
    if top and preferred and _ident(preferred[0]) not in shown:
        extras.append(
            "**Cheapest on your preferred days** (out %s, back %s): %s"
            % (_span(h.preferred_outbound), _span(h.preferred_return), _short(preferred[0]))
        )
    previous = (doc.get("reports") or [])[-1:] or []
    if top and previous and previous[0].get("best"):
        before, after = float(previous[0]["best"]), top[0]["total"]
        if abs(after - before) < 1:
            extras.append("The cheapest trip costs the same as in the last report (%s)." % _money(after))
        else:
            extras.append(
                "The cheapest trip is %s %s than in the last report (%s, now %s)."
                % (_money(abs(after - before)), "cheaper" if after < before else "more expensive",
                   _money(before), _money(after))
            )
    if extras:
        lines.append("\n".join(extras))

    body = "\n\n".join(lines)
    if len(body) > 4000:
        # Discord caps embed descriptions at 4096. Drop the links wholesale,
        # as the weekly report does, rather than cut a URL in half.
        import re

        body = re.sub(r"\[(\$\d+)\]\([^)]*\)", r"\1", body)[:4000]

    age = hours_since(doc.get("last_scan"), now)
    until = parse_ts(h.until)
    footer_bits = [
        "Out %s, back %s" % (_span(h.outbound_dates), _span(h.return_dates)),
        "all LA airports",
        "nonstop, or one 8h+ daytime layover in a big city".replace("8h", "%gh" % h.layover_min_hours),
        "prices checked %s" % ("just now" if age is not None and age < 1 else
                                "%.0fh ago" % age if age is not None else "never"),
    ]
    if until:
        from zoneinfo import ZoneInfo

        local = until.astimezone(ZoneInfo(cfg.report.timezone))
        footer_bits.append("regular tracking resumes %s" % local.strftime("%a %b %-d"))
    return title, body, " · ".join(footer_bits), top
