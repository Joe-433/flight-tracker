"""Shared types for scraping backends."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..config import Band, Config


def parse_key(key: str) -> Tuple[str, str, str, int]:
    """(out_date, ret_date, arrival airport, stops) from a history key."""
    parts = key.split("|")
    out_date = parts[0] if parts else ""
    ret_date = parts[1] if len(parts) > 1 else ""
    airport = parts[2] if len(parts) > 2 else "?"
    try:
        stops = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        stops = 0
    return out_date, ret_date, airport, stops


class ScrapeError(RuntimeError):
    """The scrape failed in a way the caller should treat as 'no data'."""


@dataclass
class Offer:
    """One roundtrip fare for one specific date pair."""

    out_date: str          # YYYY-MM-DD
    ret_date: str          # YYYY-MM-DD
    price: float
    currency: str = "USD"
    airlines: List[str] = field(default_factory=list)
    url: Optional[str] = None
    stops: Optional[int] = None
    duration_minutes: Optional[int] = None

    # Outbound leg details. Google's roundtrip payload lists OUTBOUND options
    # priced for the whole trip, so the return leg's airports and times aren't
    # in the response -- only the outbound's. Anything showing these must say
    # so rather than implying it describes the return too.
    dep_airport: Optional[str] = None
    arr_airport: Optional[str] = None
    dep_time: Optional[str] = None   # "HH:MM", 24h
    arr_time: Optional[str] = None
    flight_no: Optional[str] = None  # "AA 171", or "AA 171 +1" with a connection

    @property
    def key(self) -> str:
        """History key: dates, arrival airport, stop count.

        All four matter. A $210 one-stop and a $390 nonstop on the same dates
        are different products and must not average into one price history --
        and neither must a fare into LAX and one into Burbank.
        """
        return "%s|%s|%s|%d" % (
            self.out_date,
            self.ret_date,
            self.arr_airport or "?",
            self.stops or 0,
        )

    def snapshot(self) -> dict:
        """Flat dict for state.json -- what the weekly report renders from."""
        return {
            "price": self.price,
            "stops": self.stops,
            "airlines": self.airlines,
            "dep_airport": self.dep_airport,
            "arr_airport": self.arr_airport,
            "dep_time": self.dep_time,
            "arr_time": self.arr_time,
            "flight_no": self.flight_no,
            "out_date": self.out_date,
            "ret_date": self.ret_date,
            "url": self.url,
        }

    @property
    def label(self) -> str:
        if not self.stops:
            return "nonstop"
        return "%d stop%s" % (self.stops, "" if self.stops == 1 else "s")

    @property
    def nights(self) -> int:
        a = dt.date.fromisoformat(self.out_date)
        b = dt.date.fromisoformat(self.ret_date)
        return (b - a).days


class Source:
    """A price source. Implementations must not raise for partial failure."""

    name = "base"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.errors: List[str] = []

    def sweep(
        self, cursors: Optional[Dict[str, int]] = None
    ) -> Tuple[List[Offer], Dict[str, int]]:
        """Return (offers, next_cursors).

        `cursors` lets a backend cover a slice of the search space per run and
        resume where it left off, one cursor per band. Backends that cover
        everything in one shot return the cursors unchanged.
        """
        raise NotImplementedError


def date_pairs(cfg: Config, today: Optional[dt.date] = None) -> List[Tuple[str, str]]:
    """Every (outbound, return) pair in the horizon.

    `window_days` is the span of DEPARTURE dates, starting `min_days_ahead`
    out. The return leg is free to land past the end of that span -- clamping
    it would silently drop every trip departing in the final week.
    """
    today = today or dt.date.today()
    start = today + dt.timedelta(days=cfg.search.min_days_ahead)

    pairs: List[Tuple[str, str]] = []
    for offset in range(cfg.search.window_days + 1):
        out = start + dt.timedelta(days=offset)
        for nights in sorted(cfg.search.trip_nights):
            pairs.append((out.isoformat(), (out + dt.timedelta(days=nights)).isoformat()))
    return pairs


def slice_for_run(
    pairs: List[Tuple[str, str]], cursor: int, budget: int
) -> Tuple[List[Tuple[str, str]], int]:
    """Take `budget` pairs starting at `cursor`, wrapping around."""
    if not pairs:
        return [], 0
    budget = max(1, min(budget, len(pairs)))
    cursor = cursor % len(pairs)
    picked = [pairs[(cursor + i) % len(pairs)] for i in range(budget)]
    return picked, (cursor + budget) % len(pairs)


def plan_slice(
    cfg: Config,
    cursors: Optional[Dict[str, int]] = None,
    today: Optional[dt.date] = None,
) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
    """Choose this run's (outbound, return, destination) triples.

    Two rotations, not one. The primary destination is a city MID that already
    covers a whole metro; the secondary airports are a separate, much larger
    space (every date pair times every extra airport) that gets only
    `also_check_share` of the budget. Mixing them into one cursor would let the
    secondaries, which are four times as numerous, crowd out the destination
    that actually has the nonstops.
    """
    today = today or dt.date.today()
    previous = dict(cursors or {})
    pairs = date_pairs(cfg, today)
    primary = cfg.route.destination
    extras = list(cfg.route.also_check or [])

    budget = cfg.source.pairs_per_run
    extra_budget = (
        min(budget - 1, max(1, int(round(budget * cfg.route.also_check_share))))
        if extras
        else 0
    )
    main_budget = budget - extra_budget

    picked: List[Tuple[str, str, str]] = []
    cursors_out: Dict[str, int] = {}

    bands = cfg.source.bands or [
        Band(within_days=cfg.search.min_days_ahead + cfg.search.window_days)
    ]
    buckets: List[List[Tuple[str, str]]] = [[] for _ in bands]
    for out_date, ret_date in pairs:
        lead = (dt.date.fromisoformat(out_date) - today).days
        for index, band in enumerate(bands):
            if lead <= band.within_days:
                buckets[index].append((out_date, ret_date))
                break
        else:
            buckets[-1].append((out_date, ret_date))

    total_share = sum(b.share for b in bands) or 1.0
    for index, (band, bucket) in enumerate(zip(bands, buckets)):
        if not bucket:
            continue
        share = max(1, int(round(main_budget * band.share / total_share)))
        got, cursors_out[str(index)] = slice_for_run(
            bucket, int(previous.get(str(index), 0)), share
        )
        picked.extend((out_date, ret_date, primary) for out_date, ret_date in got)

    if extra_budget and extras:
        # Airport-major order, so one run's slice samples several airports
        # rather than grinding through every date of one of them.
        combos = [
            (out_date, ret_date, airport)
            for out_date, ret_date in pairs
            for airport in extras
        ]
        got, cursors_out["alt"] = slice_for_run(
            combos, int(previous.get("alt", 0)), extra_budget
        )
        picked.extend(got)

    return picked, cursors_out

