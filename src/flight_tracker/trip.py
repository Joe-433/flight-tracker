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

QUERIES = ("nonstop", "any", "long", "abroad", "skiplagged", "skyscanner")
GOOGLE = ("nonstop", "any", "long", "abroad")
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
    """The most recent report time at or before `now`, in UTC.

    Several times a day (`report_hours`), in local wall time (`timezone`), so
    8am stays 8am across daylight saving.
    """
    from zoneinfo import ZoneInfo

    t = cfg.trip
    tz = ZoneInfo(t.timezone or cfg.report.timezone)
    local = now.astimezone(tz)
    hours = sorted(set(t.report_hours or [t.report_hour]))
    for back in (0, 1):
        day = local.date() - dt.timedelta(days=back)
        for hour in reversed(hours):
            slot = dt.datetime.combine(day, dt.time(hour), tzinfo=tz)
            if slot <= local:
                return slot.astimezone(dt.timezone.utc)
    return dt.datetime.combine(local.date() - dt.timedelta(days=1), dt.time(hours[-1]),
                               tzinfo=tz).astimezone(dt.timezone.utc)


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
    queries = [q for q in QUERIES if (q != "abroad" or t.abroad_airports)
               and (q in GOOGLE and "google" in t.sources or q in t.sources)]
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
    if minutes > t.layover_max_hours * 60:
        return None, layover  # a day in an airport (or a hostel) isn't a connection
    long_enough = t.layover_min_hours * 60 <= minutes
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
        if query_kind in ("skiplagged", "skyscanner"):
            return self._extra(direction, day, query_kind)
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
                "url": url, "source": "Google",
            })
        return _keep(found)

    def _extra(self, direction: str, day: str, query_kind: str) -> List[Dict[str, Any]]:
        """Skiplagged or Skyscanner (via AirHint) for one direction and date."""
        from .sources import extra

        t = self.cfg.trip
        home_code = t.home_code or t.home
        frm, to = (home_code, t.away) if direction == "out" else (t.away, home_code)
        arrive_at = {t.away} if direction == "out" else set(t.home_airports or [t.home])
        try:
            if query_kind == "skiplagged":
                rows = extra.skiplagged(frm, to, day, arrive_at)
            else:
                rows = extra.airhint(frm, to, day, self.cfg.search.currency)
        except extra.SourceError as exc:
            self.errors.append("%s %s %s: %s" % (query_kind, direction, day, exc))
            return []
        excluded = {a.strip().upper() for a in t.home_exclude}
        found = []
        for row in rows:
            segments = row["segments"]
            if not segments[0]["dep"].startswith(day):
                continue
            if row.get("hidden_city") and not t.hidden_city:
                continue
            if row.get("self_transfer") and not t.self_transfer:
                continue
            home_end = segments[0]["from"] if direction == "out" else segments[-1]["to"]
            if (home_end or "").upper() in excluded:
                continue
            kind, layover = classify(segments, self.cfg)
            if kind is None or not fits(direction, segments, self.cfg):
                continue
            found.append(dict(row, direction=direction, date=day, kind=kind, layover=layover))
        return _keep(found)

    def roundtrip(self, out_date: str, ret_date: str, max_stops: int) -> Optional[Dict[str, Any]]:
        """Cheapest round-trip ticket for one date pair, return already filtered.

        Airlines often price a round trip well under two one-ways, so this is
        searched for every date pair, nonstop and with up to one stop. The
        trip's fixed points go to Google as time filters on each leg, so the
        "from" price is for a return that actually fits -- Google's answer
        only details the outbound, so the return can't be checked here.
        """
        t = self.cfg.trip
        out = {"date": out_date, "from_airport": t.home, "to_airport": t.away}
        back = {"date": ret_date, "from_airport": t.away, "to_airport": t.home}
        if t.arrive_by and t.arrive_by[:10] == out_date:
            out["latest_arrival_hour"] = int(t.arrive_by[11:13])
        if t.leave_after and t.leave_after[:10] == ret_date:
            back["earliest_departure_hour"] = int(t.leave_after[11:13])
        query = self._query("round-trip", [out, back], max_stops=max_stops)
        excluded = {a.strip().upper() for a in t.home_exclude}
        best = None
        label = "roundtrip %s %s %d" % (out_date, ret_date, max_stops)
        for item, flight_no in self._results(query, label):
            price = getattr(item, "price", None)
            segments = self._segments(item)
            if not isinstance(price, (int, float)) or price <= 0 or not segments:
                continue
            if (segments[0]["from"] or "").upper() in excluded:
                continue
            kind, layover = classify(segments, self.cfg)
            if kind is None or not fits("out", segments, self.cfg):
                continue
            if best is None or price < best["price"]:
                best = {
                    "out_date": out_date, "ret_date": ret_date, "price": float(price),
                    "max_stops": max_stops, "kind": kind, "layover": layover,
                    "airlines": list(getattr(item, "airlines", []) or []),
                    "flight_no": flight_no, "segments": segments, "url": query.url(),
                    "back_after": t.leave_after[:16] if "earliest_departure_hour" in back else None,
                }
        return best


def _keep(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cheapest few of each kind, and of each special ticket type.

    Hidden-city and self-transfer fares are kept even when they don't make
    the per-kind cut, so the report can weigh them on price.
    """
    found.sort(key=lambda leg: leg["price"])
    kept: List[Dict[str, Any]] = []
    counts: Dict[Tuple[Any, ...], int] = {}
    for leg in found:
        tag = (leg["kind"], bool(leg.get("hidden_city")), bool(leg.get("self_transfer")))
        if counts.get(tag, 0) >= KEEP_PER_KIND:
            continue
        counts[tag] = counts.get(tag, 0) + 1
        kept.append(leg)
    return kept


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
        found.append(_cheaper(combo, roundtrips.get("%s|%s|0" % (o, b))))
    found.sort(key=lambda c: (c["total"], -c["nights"]))
    return found


def any_combos(doc: Dict[str, Any], cfg: Config) -> List[Dict[str, Any]]:
    """Cheapest trip per date pair with no adventure layover: the baseline.

    Either two one-way tickets or one round-trip ticket, whichever is less.
    """
    roundtrips = doc.get("roundtrips") or {}
    found = []
    for o, b in _pairs(cfg):
        outs = [l for l in legs_for(doc, cfg, "out", o) if l["kind"] != "adventure"]
        backs = [l for l in legs_for(doc, cfg, "back", b) if l["kind"] != "adventure"]
        rts = [rt for rt in (roundtrips.get("%s|%s|1" % (o, b)), roundtrips.get("%s|%s|0" % (o, b))) if rt]
        if not (outs and backs) and not rts:
            continue
        combo = _combo(outs[0], backs[0]) if outs and backs else None
        for rt in sorted(rts, key=lambda r: r["price"]):
            combo = _cheaper(combo, rt, o, b)
        found.append(combo)
    found.sort(key=lambda c: (c["total"], -c["nights"]))
    return found


def _cheaper(combo: Optional[Dict[str, Any]], rt: Optional[Dict[str, Any]],
             o: Optional[str] = None, b: Optional[str] = None) -> Dict[str, Any]:
    """The one-way pair, or the round-trip ticket if that's cheaper."""
    if not rt or (combo is not None and float(rt["price"]) >= combo["total"]):
        return combo  # type: ignore[return-value]
    o = o or combo["out_date"]
    b = b or combo["ret_date"]
    return {
        "kind": "roundtrip", "out_date": o, "ret_date": b, "roundtrip": rt,
        "total": float(rt["price"]),
        "oneways_total": combo["total"] if combo else None,
        "nights": _nights(o, b),
    }


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


def roundtrip_targets(doc: Dict[str, Any], cfg: Config) -> List[Tuple[str, str, int]]:
    """Every date pair, nonstop and up to one stop: (out, back, max_stops).

    Capped at `roundtrip_checks` date pairs, longest stays first.
    """
    pairs = sorted(_pairs(cfg), key=lambda p: -_nights(*p))[: cfg.trip.roundtrip_checks]
    return [(o, b, stops) for o, b in pairs for stops in (0, 1)]


def shown_legs(sections: Any) -> List[Dict[str, Any]]:
    """Every one-way leg that appears in the report, once."""
    seen, legs = set(), []
    groups = sections.values() if isinstance(sections, dict) else [sections]
    for combos in groups:
        for combo in combos:
            if combo["kind"] != "oneways":
                continue
            for leg in (combo["out"], combo["back"]):
                ident = (_leg_id(leg), leg["price"])
                if ident not in seen:
                    seen.add(ident)
                    legs.append(leg)
    return legs


def _signature(combo: Dict[str, Any]) -> Tuple[Any, ...]:
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        return ("rt", combo["out_date"], combo["ret_date"], rt.get("max_stops"), rt["price"])
    return ("ow", _leg_id(combo["out"]), _leg_id(combo["back"]))


def _stops(combo: Dict[str, Any]) -> int:
    """Stops in the trip, both ways; a round trip's return counts at its limit."""
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        return len(rt["segments"]) - 1 + (rt.get("max_stops") or 0)
    return len(combo["out"]["segments"]) + len(combo["back"]["segments"]) - 2


def ranked(doc: Dict[str, Any], cfg: Config) -> List[Dict[str, Any]]:
    """Every distinct trip worth listing, cheapest first: what the report shows.

    Price comes first. A round-trip ticket competes on equal terms with every
    one-way pair, so when round trips are cheaper the list is mostly round
    trips. Ties go to fewer stops, then the longer stay. Each date pair puts
    forward its cheapest pair of any kind, its cheapest nonstop pair, both
    round-trip tickets, and its day-out layover trips, so a nonstop or a
    layover adventure still places when it's close on price.
    """
    roundtrips = doc.get("roundtrips") or {}
    candidates: List[Dict[str, Any]] = []
    for o, b in _pairs(cfg):
        outs, backs = legs_for(doc, cfg, "out", o), legs_for(doc, cfg, "back", b)
        pair = _combo(outs[0], backs[0]) if outs and backs else None
        if pair:
            candidates.append(pair)
        n_out = [l for l in outs if l["kind"] == "nonstop"]
        n_back = [l for l in backs if l["kind"] == "nonstop"]
        if n_out and n_back:
            candidates.append(_combo(n_out[0], n_back[0]))
        for stops in (0, 1):
            rt = roundtrips.get("%s|%s|%d" % (o, b, stops))
            if rt:
                combo = _cheaper(None, rt, o, b)
                combo["oneways_total"] = pair["total"] if pair else None
                candidates.append(combo)
    candidates += adventure_combos(doc, cfg, 20)
    seen, out = set(), []
    uses: Dict[Tuple[Any, ...], int] = {}
    held: List[Dict[str, Any]] = []
    for combo in sorted(candidates, key=lambda c: (c["total"], _stops(c), -c["nights"])):
        sig = _signature(combo)
        if sig in seen:
            continue
        seen.add(sig)
        # One flight paired with every return isn't five choices, it's one.
        # A leg can lead at most two entries until the rest of the list is
        # used up; extras wait at the back, still in price order.
        legs = [] if combo["kind"] == "roundtrip" else [_leg_id(combo["out"]), _leg_id(combo["back"])]
        if any(uses.get(leg, 0) >= 2 for leg in legs):
            held.append(combo)
            continue
        for leg in legs:
            uses[leg] = uses.get(leg, 0) + 1
        out.append(combo)
    return out + held


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


def _codes(leg: Dict[str, Any]) -> str:
    """Airline codes on the leg, in order: "UA", "F9/Y4"."""
    codes: List[str] = []
    for part in (leg.get("flight_no") or "").split("/"):
        code = part.strip().split(" ", 1)[0].upper()
        if re.fullmatch(r"[A-Z0-9]{2}", code) and code not in codes:
            codes.append(code)
    for name in leg.get("airlines") or []:
        key = name.strip().lower()
        code = AIRLINE_CODES.get(key) or next(
            (c for n, c in AIRLINE_CODES.items() if key.startswith(n)), name.strip()[:6])
        if code and code not in codes and len(codes) < len(leg["segments"]):
            codes.append(code)
    return "/".join(codes) or "?"


def _flags(leg: Dict[str, Any]) -> str:
    lay = leg.get("layover") or {}
    marks = [("s", leg.get("self_transfer")), ("h", leg.get("hidden_city")),
             ("o", lay.get("overnight")), ("d", leg.get("kind") == "adventure")]
    return "".join(m for m, on in marks if on)


def _cell(leg: Dict[str, Any]) -> str:
    """Out/Back column: "UA direct", "AA MIA", "F9/Y4 MCO s"."""
    segs = leg["segments"]
    where = "direct" if len(segs) == 1 else "/".join(s["to"] for s in segs[:-1])
    return " ".join(x for x in (_codes(leg), where, _flags(leg)) if x)


def _short_day(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%a %-m/%-d")


def _table(options: List[Dict[str, Any]]) -> str:
    rows = [("#", "Price", "Leave", "Return", "Out", "Back")]
    for n, combo in enumerate(options, 1):
        if combo["kind"] == "roundtrip":
            rt = combo["roundtrip"]
            out = _cell(dict(rt, date=rt["out_date"], kind=rt.get("kind") or "nonstop"))
            back = "same ticket"
        else:
            out, back = _cell(combo["out"]), _cell(combo["back"])
        rows.append((str(n), _money(combo["total"]), _short_day(combo["out_date"]),
                     _short_day(combo["ret_date"]), out, back))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    return "```\n%s\n```" % "\n".join(lines)


LEGEND = ("direct = nonstop, otherwise the stop airport · **s** separate tickets · "
          "**h** hidden-city · **o** overnight stop · **d** long daytime layover")


def _leg_line(leg: Dict[str, Any], heading: str, hints: Dict[str, Any]) -> str:
    """One leg in the details: link, times, stop, where it was found, warnings."""
    segs = leg["segments"]
    price = _money(leg["price"])
    if leg.get("url"):
        price = "[%s](%s)" % (price, leg["url"])
    bits = ["%s %s %s %s → %s %s" % (
        heading, price, segs[0]["from"], _clock(segs[0]["dep"]),
        segs[-1]["to"], _clock(segs[-1]["arr"], leg["date"]))]
    if leg.get("layover"):
        bits.append(_stop_text(leg))
    if leg.get("source") and leg["source"] != "Google":
        via = " via %s" % leg["agent"] if leg.get("agent") and leg["source"] == "Skyscanner" else ""
        bits.append("found on %s%s" % (leg["source"], via))
    if leg.get("hidden_city"):
        bits.append("⚠️ ticket is to %s and you get off at %s" % (leg.get("ticket_to") or "?", segs[-1]["to"]))
    if leg.get("self_transfer"):
        bits.append("⚠️ separate tickets")
    hint = _hint_text(hints.get(hint_key(leg)))
    if hint:
        bits.append(hint.replace(", unlikely to get cheaper", ""))
    return " · ".join(bits)


def _dates(combo: Dict[str, Any]) -> str:
    return "%s → %s, %d nights" % (_day(combo["out_date"]), _day(combo["ret_date"]), combo["nights"])


def _block(n: int, combo: Dict[str, Any], hints: Dict[str, Any]) -> str:
    """Details for row n, under the table."""
    head = "**%d.**" % n if n else ""
    if combo["kind"] == "roundtrip":
        rt = combo["roundtrip"]
        segs = rt["segments"]
        price = "[%s](%s)" % (_money(rt["price"]), rt["url"]) if rt.get("url") else _money(rt["price"])
        saving = ""
        if combo.get("oneways_total"):
            diff = combo["oneways_total"] - rt["price"]
            if diff >= 1:
                saving = " · %s less than two one-ways" % _money(diff)
            elif diff <= -1:
                saving = " · %s more than two one-ways" % _money(-diff)
        back_rule = "nonstop" if rt.get("max_stops") == 0 else "nonstop or 1 stop"
        if rt.get("back_after"):
            back_rule += ", after %s" % _clock(rt["back_after"])
        return "%s Round-trip ticket %s on %s%s\n Out %s %s → %s %s · pick the return when booking (%s)" % (
            head, price, ", ".join(rt.get("airlines") or []) or "?", saving,
            segs[0]["from"], _clock(segs[0]["dep"]), segs[-1]["to"],
            _clock(segs[-1]["arr"], rt["out_date"]), back_rule)
    return "%s %s + %s = %s\n %s\n %s" % (
        head, _money(combo["out"]["price"]), _money(combo["back"]["price"]), _money(combo["total"]),
        _leg_line(combo["out"], "Out", hints), _leg_line(combo["back"], "Back", hints))


def _short(combo: Dict[str, Any]) -> str:
    if combo["kind"] == "roundtrip":
        return "%s round-trip ticket on %s, %s" % (
            _money(combo["total"]), ", ".join(combo["roundtrip"].get("airlines") or []) or "?", _dates(combo))
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
    """The report: the cheapest `max_options` trips, as one (title, body, footer)."""
    t = cfg.trip
    hints = doc.get("hints") or {}
    options = ranked(doc, cfg)[: t.max_options]
    parts = sections(doc, cfg)

    take: List[str] = []
    if options:
        best = options[0]
        how = "one round-trip ticket" if best["kind"] == "roundtrip" else "two one-way tickets"
        take.append("Cheapest: **%s** (%s)." % (_short(best), how))
        rts = [c for c in options if c["kind"] == "roundtrip"]
        pairs = [c for c in ranked(doc, cfg) if c["kind"] == "oneways"]
        if rts and pairs and rts[0]["total"] < pairs[0]["total"]:
            take.append("Round-trip tickets are cheaper right now: the best is %s under the "
                        "cheapest two one-ways (%s)." % (
                            _money(pairs[0]["total"] - rts[0]["total"]), _money(pairs[0]["total"])))
    shown = {_signature(c) for c in options}
    if parts["nonstop"] and not any(_stops(c) == 0 for c in options):
        take.append("Cheapest nonstop both ways (not in the list): %s." % _short(parts["nonstop"][0]))
    if parts["adventure"] and not any(_signature(c) in shown for c in parts["adventure"]):
        a = parts["adventure"][0]
        star = a[a["star"]]
        take.append("Cheapest with a day out (not in the list): %s, %s in %s." % (
            _short(a), _hours(star["layover"]["minutes"]), star["layover"]["city"]))
    previous = (doc.get("reports") or [])[-1:] or [{}]
    before = (previous[0].get("best") or {}).get("top")
    if options and before:
        change = options[0]["total"] - float(before)
        if abs(change) >= 1:
            take.append("Since the last report the cheapest trip is %s %s (was %s)." % (
                _money(abs(change)), "cheaper" if change < 0 else "more expensive", _money(before)))
    if not options:
        take.append("No complete trip found yet: the last search didn't return a usable "
                    "flight in both directions.")

    age = hours_since(doc.get("last_scan"), now)
    names = {"google": "Google Flights", "skiplagged": "Skiplagged", "skyscanner": "Skyscanner (via AirHint)"}
    footer = " \u00b7 ".join([
        "Out %s, back %s" % (_span(t.outbound_dates), _span(t.return_dates)),
        "base fares from %s" % ", ".join(names.get(x, x) for x in t.sources),
        "checked %s" % ("just now" if age is not None and age < 1 else
                         "%.0fh ago" % age if age is not None else "never"),
    ])
    body = ["\n".join(take)]
    if options:
        body += [_table(options), LEGEND, "\n".join(_block(i, c, hints) for i, c in enumerate(options, 1))]
    return [("%s flights \u00b7 the %d cheapest" % (t.name, len(options) or t.max_options),
             _fit("\n\n".join(body)), footer)]


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
    """Cheapest total per section and overall ("top"), for history and alerts."""
    found = {key: combos[0]["total"] for key, combos in sections(doc, cfg).items() if combos}
    top = ranked(doc, cfg)[:1]
    if top:
        found["top"] = top[0]["total"]
    return found


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
