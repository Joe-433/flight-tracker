"""Trip mode: watch one specific trip, as two one-way legs with flexible dates.

The regular sweep watches roundtrips of a fixed length on one route. A real
trip is a different shape: you have to be somewhere by a certain time, can
leave after a certain time, and are flexible by a day or so on each end. So
this searches each direction on each candidate date, four ways:

    nonstop   no stops
    any       one stop, whatever Google ranks first
    long      one stop with a layover of N+ hours (Google filters server-side)
    abroad    the same, but only through a list of airports outside the US

The last two are the point. Google's default results hide most long layovers
behind "more flights"; asking for them by layover length surfaces a daytime
stop in San Salvador or Montreal that a plain search never shows.

Every itinerary is sorted into one of three kinds:

    nonstop     one segment
    adventure   one stop that is a real daytime visit: `layover_min_hours` to
                `layover_max_hours`, same airport in and out, all of it between
                `layover_day_hours` on one calendar day
    regular     any other one-stop

and must fit the trip: outbound lands by `arrive_by`, return leaves after
`leave_after`. The report pairs outbound and return legs into round-trip
totals (out + back = total): the cheapest nonstop trips, the cheapest trips
with an adventure layover, and the cheapest of anything for comparison.
AirHint (airhint.py) adds a buy-or-wait call to each leg shown.

Three stages, like the regular sweep: plan (is a scan due?), sweep (one
shard's searches, own runner), apply (merge, roundtrip checks, AirHint, rank,
report, save). State lives in data/trip.json.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .state import hours_since, parse_ts, utcnow

DEFAULT_STATE = os.path.join("data", "trip.json")

QUERIES = ("nonstop", "any", "long", "abroad")
KEEP_PER_KIND = 4

# (direction, date, query). "out" flies home -> away, "back" away -> home.
Item = Tuple[str, str, str]

# Where a layover is. Anything not listed shows as its airport code.
PLACES: Dict[str, Tuple[str, str]] = {
    "ATL": ("Atlanta", "US"), "BOS": ("Boston", "US"), "CLT": ("Charlotte", "US"),
    "DCA": ("Washington DC", "US"), "DEN": ("Denver", "US"), "DFW": ("Dallas", "US"),
    "FLL": ("Fort Lauderdale", "US"), "IAD": ("Washington DC", "US"),
    "IAH": ("Houston", "US"), "LAX": ("Los Angeles", "US"), "MCO": ("Orlando", "US"),
    "MIA": ("Miami", "US"), "ORD": ("Chicago", "US"), "PHL": ("Philadelphia", "US"),
    "TPA": ("Tampa", "US"), "SJU": ("San Juan", "Puerto Rico"),
    "BOG": ("Bogotá", "Colombia"), "MDE": ("Medellín", "Colombia"),
    "CUN": ("Cancún", "Mexico"), "MEX": ("Mexico City", "Mexico"),
    "GDL": ("Guadalajara", "Mexico"), "MTY": ("Monterrey", "Mexico"),
    "PTY": ("Panama City", "Panama"), "SAL": ("San Salvador", "El Salvador"),
    "SJO": ("San José", "Costa Rica"), "LIR": ("Liberia", "Costa Rica"),
    "SDQ": ("Santo Domingo", "Dominican Republic"),
    "PUJ": ("Punta Cana", "Dominican Republic"),
    "SAP": ("San Pedro Sula", "Honduras"), "YUL": ("Montreal", "Canada"),
    "YYZ": ("Toronto", "Canada"), "BZE": ("Belize City", "Belize"),
}

# Airline name -> IATA code, for AirHint, when the flight number is missing.
AIRLINE_CODES = {
    "american": "AA", "united": "UA", "delta": "DL", "jetblue": "B6",
    "spirit": "NK", "frontier": "F9", "alaska": "AS", "southwest": "WN",
    "avianca": "AV", "copa": "CM", "aeromexico": "AM", "aeroméxico": "AM",
    "volaris": "Y4", "air canada": "AC", "arajet": "DM", "viva": "VB",
}


# ---------------------------------------------------------------------------
# mode and timing
# ---------------------------------------------------------------------------


def active(cfg: Config, now: Optional[dt.datetime] = None) -> bool:
    """Is a trip being watched right now? Off once `until` has passed."""
    t = cfg.trip
    until = parse_ts(t.until)
    if until is None or not t.away or not t.home:
        return False
    return (now or utcnow()) < until


def regular_paused(cfg: Config, now: Optional[dt.datetime] = None) -> bool:
    until = parse_ts(cfg.pause.regular_until)
    return until is not None and (now or utcnow()) < until


def _days(span: List[str]) -> List[str]:
    if not span:
        return []
    first = dt.date.fromisoformat(span[0])
    last = dt.date.fromisoformat(span[-1])
    return [(first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def report_slot(cfg: Config, now: dt.datetime) -> dt.datetime:
    """Today's report time if it has passed, else yesterday's. In UTC."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.report.timezone)
    local = now.astimezone(tz)
    slot = dt.datetime.combine(local.date(), dt.time(cfg.trip.report_hour), tzinfo=tz)
    if slot > local:
        slot = dt.datetime.combine(
            local.date() - dt.timedelta(days=1), dt.time(cfg.trip.report_hour), tzinfo=tz
        )
    return slot.astimezone(dt.timezone.utc)


def report_due(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> bool:
    sent = parse_ts(doc.get("last_report"))
    return sent is None or sent < report_slot(cfg, now)


def scan_due(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> bool:
    age = hours_since(doc.get("last_scan"), now)
    return age is None or age >= cfg.trip.scan_every_hours


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
    t = cfg.trip
    queries = [q for q in QUERIES if q != "abroad" or t.abroad_airports]
    out = [("out", day, q) for day in _days(t.outbound_dates) for q in queries]
    back = [("back", day, q) for day in _days(t.return_dates) for q in queries]
    return out + back


def make_plan(cfg: Config, doc: Dict[str, Any], now: Optional[dt.datetime] = None,
              force: bool = False) -> Dict[str, Any]:
    """Decide whether this run scans, and split the searches into shards.

    A scan runs every `scan_every_hours`. A due report forces a fresh one, but
    at most hourly: if Discord is down, the report retries without re-running
    every search each half hour.
    """
    now = now or utcnow()
    due_report = report_due(doc, cfg, now)
    age = hours_since(doc.get("last_scan"), now)
    fresh_enough = age is not None and age < 1
    due = force or scan_due(doc, cfg, now) or (due_report and not fresh_enough)
    shards: List[List[Item]] = [[] for _ in range(max(1, cfg.trip.shards))]
    if due:
        for i, item in enumerate(items(cfg)):
            # Offset by date so each shard gets a mix of search kinds: one
            # dead runner must not take out every nonstop search.
            shards[(i + i // len(QUERIES)) % len(shards)].append(item)
    return {"created": now.isoformat(), "scan": due, "report_due": due_report, "shards": shards}


# ---------------------------------------------------------------------------
# classifying an itinerary
# ---------------------------------------------------------------------------


def _stamp(moment: Any) -> Optional[str]:
    """"YYYY-MM-DDTHH:MM" from a fast-flights SimpleDatetime, tolerantly."""
    try:
        y, m, d = (int(x) for x in moment.date)
        hh, mm = (int(x) for x in (list(moment.time) + [0, 0])[:2])
        return "%04d-%02d-%02dT%02d:%02d" % (y, m, d, hh, mm)
    except (AttributeError, TypeError, ValueError):
        return None


def classify(segments: List[Dict[str, Any]], cfg: Config) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(kind, layover) for an itinerary: nonstop / adventure / regular / None.

    None means unusable: two or more stops, an airport change mid-layover, or
    times missing.
    """
    if len(segments) == 1:
        return "nonstop", None
    if len(segments) != 2:
        return None, None
    first, second = segments
    airport = (first.get("to") or "").upper()
    if not airport or (second.get("from") or "").upper() != airport:
        return None, None
    land, leave = first.get("arr"), second.get("dep")
    if not land or not leave:
        return None, None

    start, end = dt.datetime.fromisoformat(land), dt.datetime.fromisoformat(leave)
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0:
        return None, None
    t = cfg.trip
    day_start, day_end = (list(t.layover_day_hours) + [6, 24])[:2]
    overnight = start.date() != end.date() or start.hour < day_start
    daytime = not overnight and (end.hour + end.minute / 60.0) <= day_end
    city, country = PLACES.get(airport, (airport, ""))
    layover = {
        "airport": airport, "city": city, "country": country,
        "abroad": bool(country) and country != "US",
        "minutes": minutes, "start": land, "end": leave, "overnight": overnight,
    }
    long_enough = t.layover_min_hours * 60 <= minutes <= t.layover_max_hours * 60
    return ("adventure" if (long_enough and daytime) else "regular"), layover


def fits(direction: str, segments: List[Dict[str, Any]], cfg: Config) -> bool:
    """Does this leg fit the trip's fixed points?"""
    t = cfg.trip
    if direction == "out" and t.arrive_by:
        arr = segments[-1].get("arr")
        return bool(arr) and arr <= t.arrive_by[:16]
    if direction == "back" and t.leave_after:
        dep = segments[0].get("dep")
        return bool(dep) and dep >= t.leave_after[:16]
    return True


# ---------------------------------------------------------------------------
# sweep (network)
# ---------------------------------------------------------------------------


class TripSource:
    """One-way and nonstop-roundtrip searches through fast-flights.

    Reuses the regular PairsSource's fetch/parse/flight-number plumbing; only
    the query shape and the acceptance rules differ.
    """

    def __init__(self, cfg: Config) -> None:
        from .sources.pairs import PairsSource

        self.cfg = cfg
        self.pairs = PairsSource(cfg)
        self.errors: List[str] = []

    def _query(self, trip: str, legs: List[Dict[str, Any]], max_stops: int) -> Any:
        s = self.cfg.search
        p = self.pairs
        return p._create_query(
            flights=[p._FlightQuery(**leg) for leg in legs],
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
        from .sources.pairs import _format_flight_no, carrier_matches

        p = self.pairs
        p.errors = []
        results = p._fetch(query, label, "", self.cfg.trip.away)
        if p.errors:
            self.errors.extend(p.errors)
        if not results:
            return []
        numbers = p._numbers if len(p._numbers or []) == len(results) else []
        out = []
        for i, item in enumerate(results):
            flight_no = None
            if i < len(numbers):
                candidate = _format_flight_no(numbers[i], len(getattr(item, "flights", []) or []))
                if carrier_matches(candidate, list(getattr(item, "airlines", []) or [])):
                    flight_no = candidate
            out.append((item, flight_no))
        return out

    def _segments(self, item: Any) -> List[Dict[str, Any]]:
        return [{
            "from": getattr(getattr(f, "from_airport", None), "code", None),
            "to": getattr(getattr(f, "to_airport", None), "code", None),
            "dep": _stamp(getattr(f, "departure", None)),
            "arr": _stamp(getattr(f, "arrival", None)),
        } for f in list(getattr(item, "flights", []) or [])]

    def leg(self, direction: str, day: str, query_kind: str) -> List[Dict[str, Any]]:
        """The cheapest few itineraries of each kind from one search."""
        t = self.cfg.trip
        frm, to = (t.home, t.away) if direction == "out" else (t.away, t.home)
        spec: Dict[str, Any] = {"date": day, "from_airport": frm, "to_airport": to}
        if query_kind in ("long", "abroad"):
            spec["min_layover_minutes"] = int(t.layover_min_hours * 60)
            spec["max_layover_minutes"] = int(t.layover_max_hours * 60)
        if query_kind == "abroad":
            spec["connecting_airports"] = list(t.abroad_airports)
        query = self._query("one-way", [spec], max_stops=0 if query_kind == "nonstop" else 1)
        url = query.url()
        label = "%s %s %s" % (direction, day, query_kind)
        excluded = {a.strip().upper() for a in t.home_exclude}

        found: List[Dict[str, Any]] = []
        for item, flight_no in self._results(query, label):
            price = getattr(item, "price", None)
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            segments = self._segments(item)
            if not segments:
                continue
            home_end = segments[0]["from"] if direction == "out" else segments[-1]["to"]
            if (home_end or "").upper() in excluded:
                continue
            kind, layover = classify(segments, self.cfg)
            if kind is None or not fits(direction, segments, self.cfg):
                continue
            found.append({
                "direction": direction, "date": day, "kind": kind,
                "price": float(price),
                "airlines": list(getattr(item, "airlines", []) or []),
                "flight_no": flight_no, "segments": segments, "layover": layover,
                "url": url,
            })
        found.sort(key=lambda leg: leg["price"])
        kept: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        for leg in found:
            if counts.get(leg["kind"], 0) >= KEEP_PER_KIND:
                continue
            counts[leg["kind"]] = counts.get(leg["kind"], 0) + 1
            kept.append(leg)
        return kept

    def roundtrip(self, out_date: str, ret_date: str, home_airport: str) -> Optional[Dict[str, Any]]:
        """Cheapest nonstop roundtrip on one ticket, from a named home airport.

        Named, not the city code: the payload only details the outbound, so a
        city-wide return could quietly land at a different airport.
        """
        away = self.cfg.trip.away
        query = self._query("round-trip", [
            {"date": out_date, "from_airport": home_airport, "to_airport": away},
            {"date": ret_date, "from_airport": away, "to_airport": home_airport},
        ], max_stops=0)
        best = None
        for item, flight_no in self._results(query, "roundtrip %s %s" % (out_date, ret_date)):
            price = getattr(item, "price", None)
            segments = self._segments(item)
            if not isinstance(price, (int, float)) or price <= 0 or len(segments) != 1:
                continue
            if not fits("out", segments, self.cfg):
                continue
            if best is None or price < best["price"]:
                best = {
                    "out_date": out_date, "ret_date": ret_date, "price": float(price),
                    "airlines": list(getattr(item, "airlines", []) or []),
                    "flight_no": flight_no, "segments": segments, "url": query.url(),
                }
        return best


def _pause(cfg: Config, first: bool) -> None:
    if first:
        return
    lo, hi = (list(cfg.source.jitter_seconds) + [0, 0])[:2]
    time.sleep(random.uniform(float(lo), float(hi)))


def item_key(direction: str, day: str, query_kind: str) -> str:
    return "%s|%s|%s" % (direction, day, query_kind)


def run_shard(cfg: Config, plan: Dict[str, Any], shard: int, source: Any = None) -> Dict[str, Any]:
    shards = plan.get("shards") or []
    todo = shards[shard] if 0 <= shard < len(shards) else []
    source = source or TripSource(cfg)
    results: Dict[str, List[Dict[str, Any]]] = {}
    failed: List[str] = []
    for i, (direction, day, query_kind) in enumerate(todo):
        _pause(cfg, i == 0)
        before = len(source.errors)
        found = source.leg(direction, day, query_kind)
        key = item_key(direction, day, query_kind)
        # A search that errored (blocked, rate-limited, unparsable) says
        # nothing about the flights, so it must not wipe the last good ones.
        # "No flights found" is Google answering: that one is a real empty.
        new_errors = source.errors[before:]
        if new_errors and not found and not all("FlightsNotFound" in e for e in new_errors):
            failed.append(key)
            continue
        results[key] = found
    return {"shard": shard, "results": results, "failed": failed, "errors": list(source.errors)}


# ---------------------------------------------------------------------------
# combine and rank
# ---------------------------------------------------------------------------


def merge(doc: Dict[str, Any], shard_results: List[Dict[str, Any]], now: dt.datetime) -> Tuple[int, int]:
    """Fold shard results into doc["searches"]. Returns (searched, with a fare).

    A searched key is replaced wholesale, including by nothing; a key whose
    search failed keeps its last known legs.
    """
    searches: Dict[str, List[Dict[str, Any]]] = dict(doc.get("searches") or {})
    searched = found = 0
    for result in shard_results:
        for key, legs in (result.get("results") or {}).items():
            searched += 1
            found += 1 if legs else 0
            if not legs and searches.get(key):
                # Google sometimes says "no flights" for a search that had
                # some minutes ago. Keep the old ones; legs_for() stops
                # quoting them once they're a couple of scans old.
                continue
            searches[key] = [dict(leg, seen=now.isoformat()) for leg in legs]
    doc["searches"] = searches
    return searched, found


def _leg_id(leg: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple((s.get("from"), s.get("to"), s.get("dep")) for s in leg["segments"])


def legs_for(doc: Dict[str, Any], cfg: Config, direction: str, day: str,
             kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every distinct leg seen for this direction and date, cheapest first.

    The four searches overlap, and the same flight can come back from two of
    them; it's kept once, at its lowest price. Legs are re-checked against the
    current config, so tightening `arrive_by` takes effect without a rescan.
    """
    now = parse_ts(doc.get("last_scan"))
    best: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for query_kind in QUERIES:
        for leg in (doc.get("searches") or {}).get(item_key(direction, day, query_kind), []):
            age = hours_since(leg.get("seen"), now) if now else None
            if age is not None and age > 2.5 * cfg.trip.scan_every_hours:
                continue  # its search has been failing; too old to quote
            leg_kind, layover = classify(leg["segments"], cfg)
            if leg_kind is None or not fits(direction, leg["segments"], cfg):
                continue
            if kind and leg_kind != kind:
                continue
            leg = dict(leg, kind=leg_kind, layover=layover)
            ident = _leg_id(leg)
            if ident not in best or leg["price"] < best[ident]["price"]:
                best[ident] = leg
    return sorted(best.values(), key=lambda leg: (leg["price"], leg["segments"][0].get("dep") or ""))


def _nights(out_date: str, ret_date: str) -> int:
    return (dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)).days


def _combo(out_leg: Dict[str, Any], back_leg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "oneways", "out_date": out_leg["date"], "ret_date": back_leg["date"],
        "out": out_leg, "back": back_leg,
        "total": float(out_leg["price"]) + float(back_leg["price"]),
        "nights": _nights(out_leg["date"], back_leg["date"]),
    }


def _pairs(cfg: Config) -> List[Tuple[str, str]]:
    return [(o, b) for o in _days(cfg.trip.outbound_dates) for b in _days(cfg.trip.return_dates) if b > o]


def nonstop_combos(doc: Dict[str, Any], cfg: Config) -> List[Dict[str, Any]]:
    """Cheapest nonstop-both-ways trip per date pair, best first."""
    roundtrips = doc.get("roundtrips") or {}
    found = []
    for o, b in _pairs(cfg):
        outs, backs = legs_for(doc, cfg, "out", o, "nonstop"), legs_for(doc, cfg, "back", b, "nonstop")
        if not outs or not backs:
            continue
        combo = _combo(outs[0], backs[0])
        rt = roundtrips.get("%s|%s" % (o, b))
        # A roundtrip price is for the cheapest return that day, whenever it
        # leaves; only trust it on days when every departure fits.
        anytime = not cfg.trip.leave_after or (b + "T00:00") >= cfg.trip.leave_after[:16]
        if rt and anytime and float(rt["price"]) < combo["total"]:
            combo = {
                "kind": "roundtrip", "out_date": o, "ret_date": b, "roundtrip": rt,
                "total": float(rt["price"]), "oneways_total": combo["total"],
                "nights": _nights(o, b),
            }
        found.append(combo)
    found.sort(key=lambda c: (c["total"], -c["nights"]))
    return found


def any_combos(doc: Dict[str, Any], cfg: Config) -> List[Dict[str, Any]]:
    """Cheapest trip per date pair with no adventure layover: the baseline."""
    found = []
    for o, b in _pairs(cfg):
        outs = [l for l in legs_for(doc, cfg, "out", o) if l["kind"] != "adventure"]
        backs = [l for l in legs_for(doc, cfg, "back", b) if l["kind"] != "adventure"]
        if outs and backs:
            found.append(_combo(outs[0], backs[0]))
    found.sort(key=lambda c: (c["total"], -c["nights"]))
    return found


def adventure_combos(doc: Dict[str, Any], cfg: Config, limit: int) -> List[Dict[str, Any]]:
    """Cheapest trips with a long daytime layover, one per layover city.

    Each adventure leg is paired with the cheapest leg of any kind going the
    other way. The list is a menu of different days out, so a city appears
    once per direction, and if there's a layover in another country that
    didn't make the cut on price alone, it takes the last slot.
    """
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for o, b in _pairs(cfg):
        outs, backs = legs_for(doc, cfg, "out", o), legs_for(doc, cfg, "back", b)
        if not outs or not backs:
            continue
        candidates = [(leg, backs[0], leg) for leg in outs if leg["kind"] == "adventure"]
        candidates += [(outs[0], leg, leg) for leg in backs if leg["kind"] == "adventure"]
        for out_leg, back_leg, star in candidates:
            combo = _combo(out_leg, back_leg)
            combo["star"] = star["direction"]
            combo["abroad"] = bool(star["layover"]["abroad"])
            ident = (star["direction"], star["layover"]["airport"])
            current = best.get(ident)
            if current is None or (combo["total"], -combo["nights"]) < (current["total"], -current["nights"]):
                best[ident] = combo
    ranked = sorted(best.values(), key=lambda c: (c["total"], -c["nights"]))
    top = ranked[:limit]
    if top and not any(c["abroad"] for c in top):
        abroad = [c for c in ranked[limit:] if c["abroad"]]
        if abroad:
            top[-1] = abroad[0]
    return top


def roundtrip_targets(doc: Dict[str, Any], cfg: Config) -> List[Tuple[str, str, str]]:
    """Date pairs worth a nonstop roundtrip search, with the home airport."""
    targets: List[Tuple[str, str, str]] = []
    saved = doc.get("roundtrips")
    doc["roundtrips"] = {}  # rank by one-ways alone
    try:
        ranked = nonstop_combos(doc, cfg)
    finally:
        doc["roundtrips"] = saved or {}
    for combo in ranked:
        if len(targets) >= cfg.trip.roundtrip_checks:
            break
        target = (combo["out_date"], combo["ret_date"], combo["out"]["segments"][0]["from"])
        if target not in targets:
            targets.append(target)
    return targets


def shown_legs(sections: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Every one-way leg that appears in the report, once."""
    seen, legs = set(), []
    for combos in sections.values():
        for combo in combos:
            if combo["kind"] != "oneways":
                continue
            for leg in (combo["out"], combo["back"]):
                ident = (_leg_id(leg), leg["price"])
                if ident not in seen:
                    seen.add(ident)
                    legs.append(leg)
    return legs


def sections(doc: Dict[str, Any], cfg: Config) -> Dict[str, List[Dict[str, Any]]]:
    n = cfg.trip.per_section
    return {
        "nonstop": nonstop_combos(doc, cfg)[:n],
        "adventure": adventure_combos(doc, cfg, n),
        "any": any_combos(doc, cfg)[:1],
    }


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


def airline_code(leg: Dict[str, Any]) -> Optional[str]:
    """IATA code of the leg's first airline, for AirHint."""
    flight_no = leg.get("flight_no") or ""
    code = flight_no.split(" ", 1)[0].strip().upper()
    if re.fullmatch(r"[A-Z0-9]{2}", code or ""):
        return code
    for name in leg.get("airlines") or []:
        code = AIRLINE_CODES.get(name.strip().lower())
        if code:
            return code
    return None


def hint_key(leg: Dict[str, Any]) -> str:
    segs = leg["segments"]
    return "%s|%s|%s|%s|%d" % (airline_code(leg) or "?", segs[0]["from"], segs[-1]["to"],
                                leg["date"], round(leg["price"]))


def _hint_text(hint: Optional[Dict[str, Any]]) -> str:
    """AirHint's call in plain words, or nothing."""
    if not hint or not hint.get("suggestion"):
        return ""
    suggestion = str(hint["suggestion"]).lower()
    confidence = hint.get("confidence")
    if suggestion.startswith("wait"):
        text = "AirHint: wait"
        if isinstance(confidence, (int, float)):
            text += ", %d%% sure it drops" % confidence
        if hint.get("max_saving"):
            text += " (up to %s)" % hint["max_saving"]
        return text
    if suggestion.startswith("buy"):
        return "AirHint: buy, unlikely to get cheaper"
    return "AirHint: %s" % hint["suggestion"]


def _stop_text(leg: Dict[str, Any]) -> str:
    lay = leg.get("layover")
    if not lay:
        return "nonstop"
    where = lay["city"] + (", %s" % lay["country"] if lay.get("abroad") else "")
    span = "%s–%s" % (_clock(lay["start"]), _clock(lay["end"]))
    if leg["kind"] == "adventure":
        return "%s%s in %s (%s)" % ("\U0001F30E " if lay.get("abroad") else "", _hours(lay["minutes"]), where, span)
    return "stop in %s, %s%s" % (where, _hours(lay["minutes"]), " overnight" if lay.get("overnight") else "")


def _leg_line(leg: Dict[str, Any], heading: str, hints: Dict[str, Any]) -> str:
    segs = leg["segments"]
    airline = ", ".join(leg.get("airlines") or []) or "?"
    route = "%s %s → %s %s" % (
        segs[0]["from"], _clock(segs[0]["dep"]), segs[-1]["to"], _clock(segs[-1]["arr"], leg["date"]),
    )
    price = _money(leg["price"])
    if leg.get("url"):
        price = "[%s](%s)" % (price, leg["url"])
    line = "%s %s · %s · %s · %s · %s" % (
        heading, _day(leg["date"]), price, airline, route, _stop_text(leg))
    hint = _hint_text(hints.get(hint_key(leg)))
    return line + ("\n  *%s*" % hint if hint else "")


def _dates(combo: Dict[str, Any]) -> str:
    return "%s → %s, %d nights" % (_day(combo["out_date"]), _day(combo["ret_date"]), combo["nights"])


def _block(n: int, combo: Dict[str, Any], hints: Dict[str, Any]) -> str:
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        seg = rt["segments"][0]
        price = "[%s](%s)" % (_money(rt["price"]), rt["url"]) if rt.get("url") else _money(rt["price"])
        return (
            "**%d. %s round trip** · %s\n"
            "One ticket on %s, %s cheaper than two one-ways. %s\n"
            "Out %s %s → %s %s nonstop; pick the nonstop return when booking."
            % (n, _money(rt["price"]), _dates(combo), ", ".join(rt.get("airlines") or []) or "?",
               _money(combo["oneways_total"] - rt["price"]), price,
               seg["from"], _clock(seg["dep"]), seg["to"], _clock(seg["arr"], rt["out_date"]))
        )
    return "**%s%s + %s = %s** · %s\n%s\n%s" % (
        "%d. " % n if n else "", _money(combo["out"]["price"]), _money(combo["back"]["price"]), _money(combo["total"]),
        _dates(combo), _leg_line(combo["out"], "Out", hints), _leg_line(combo["back"], "Back", hints),
    )


def _short(combo: Dict[str, Any]) -> str:
    if combo["kind"] == "roundtrip":
        return "%s round trip, %s" % (_money(combo["total"]), _dates(combo))
    return "%s + %s = %s, %s" % (_money(combo["out"]["price"]), _money(combo["back"]["price"]),
                                 _money(combo["total"]), _dates(combo))


def _strip_links(text: str) -> str:
    return re.sub(r"\[(\$\d+)\]\([^)]*\)", r"\1", text)


def _fit(text: str) -> str:
    """Discord caps an embed description at 4096: drop links before cutting."""
    if len(text) > 4000:
        text = _strip_links(text)
    return text[:4000]


def report(doc: Dict[str, Any], cfg: Config, now: dt.datetime) -> List[Tuple[str, str, str]]:
    """The daily report as (title, body, footer) messages, one per section."""
    t = cfg.trip
    parts = sections(doc, cfg)
    hints = doc.get("hints") or {}
    nonstop, adventure, baseline = parts["nonstop"], parts["adventure"], parts["any"]

    take: List[str] = []
    cheapest = min([c for c in (baseline[:1] + nonstop[:1])], key=lambda c: c["total"], default=None)
    same = bool(nonstop) and cheapest is nonstop[0] or (
        bool(nonstop) and bool(baseline) and baseline[0]["total"] == nonstop[0]["total"])
    if cheapest and same:
        take.append("Cheapest trip is nonstop both ways: **%s**." % _short(nonstop[0]))
    else:
        if baseline:
            take.append("Cheapest trip: **%s** (with a regular connection)." % _short(baseline[0]))
        if nonstop:
            take.append("Cheapest nonstop both ways: **%s**." % _short(nonstop[0]))
    if adventure:
        star = adventure[0][adventure[0]["star"]]
        take.append("Cheapest with a day out on the way: **%s**, %s in %s." % (
            _short(adventure[0]), _hours(star["layover"]["minutes"]), star["layover"]["city"]))
    previous = (doc.get("reports") or [])[-1:] or [{}]
    for key, label in (("any", "cheapest trip"), ("nonstop", "cheapest nonstop trip")):
        combos = parts[key]
        before = (previous[0].get("best") or {}).get(key)
        if combos and before:
            change = combos[0]["total"] - float(before)
            if abs(change) >= 1:
                take.append("Since the last report the %s is %s %s (was %s)." % (
                    label, _money(abs(change)), "cheaper" if change < 0 else "more expensive", _money(before)))
    if not take:
        take.append("No complete trip found yet: the last search didn't return a usable "
                    "flight in both directions.")

    age = hours_since(doc.get("last_scan"), now)
    footer = " · ".join([
        "Out %s, back %s" % (_span(t.outbound_dates), _span(t.return_dates)),
        "prices are one-way base fares, added up",
        "checked %s" % ("just now" if age is not None and age < 1 else
                         "%.0fh ago" % age if age is not None else "never"),
    ])

    first = ["\n".join(take), "**Nonstop both ways**"]
    first += [_block(i, c, hints) for i, c in enumerate(nonstop, 1)] or [
        "No nonstop found in both directions on these dates."]
    second = ["**A day out on the way** (one %g–%gh daytime layover)" % (t.layover_min_hours, t.layover_max_hours)]
    second += [_block(i, c, hints) for i, c in enumerate(adventure, 1)] or [
        "No long daytime layover found on these dates."]
    if baseline and not same:
        second.append("**For comparison, the cheapest ordinary trip**\n" + _block(0, baseline[0], hints))
    return [
        ("%s flights · nonstop" % t.name, _fit("\n\n".join(first)), footer),
        ("%s flights · layover adventures" % t.name, _fit("\n\n".join(second)), footer),
    ]


def _span(span: List[str]) -> str:
    if not span:
        return "?"
    a, b = dt.date.fromisoformat(span[0]), dt.date.fromisoformat(span[-1])
    if a == b:
        return a.strftime("%b %-d")
    if a.month == b.month:
        return "%s–%d" % (a.strftime("%b %-d"), b.day)
    return "%s – %s" % (a.strftime("%b %-d"), b.strftime("%b %-d"))


def bests(doc: Dict[str, Any], cfg: Config) -> Dict[str, float]:
    """Cheapest total per section, for history and drop alerts."""
    return {key: combos[0]["total"] for key, combos in sections(doc, cfg).items() if combos}


def drop_alerts(doc: Dict[str, Any], cfg: Config) -> List[Tuple[str, Dict[str, Any], float]]:
    """(section, combo, baseline) where the price fell enough to say so.

    The baseline is the price the user was last told (doc["told"]), set only
    when a report or alert is actually delivered. So a slow slide adds up
    until it crosses the threshold, and an alert that failed to send is still
    owed next run. Nothing is recorded here.
    """
    told: Dict[str, float] = doc.get("told") or {}
    alerts = []
    for key, combos in sections(doc, cfg).items():
        before = told.get(key)
        if combos and before is not None and before - combos[0]["total"] >= cfg.trip.drop_alert_usd:
            alerts.append((key, combos[0], float(before)))
    return alerts
