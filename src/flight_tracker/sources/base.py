"""Shared types for scraping backends."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..config import Band, Config


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

    @property
    def key(self) -> str:
        """History key. Nonstop keeps the bare date-pair form.

        Connecting fares get a `|<stops>` suffix so they track as their own
        series -- a $210 one-stop and a $390 nonstop on the same dates are
        different products and must not average into one price history.
        """
        if not self.stops:
            return "%s|%s" % (self.out_date, self.ret_date)
        return "%s|%s|%d" % (self.out_date, self.ret_date, self.stops)

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
) -> Tuple[List[Tuple[str, str]], Dict[str, int]]:
    """Choose this run's date pairs, split across bands.

    Over a 3-month horizon a flat rotation would take most of a day to come
    back around, which is useless for the near-term dates where fares actually
    move. Each band keeps its own cursor and gets its own share of
    `pairs_per_run`, so the near horizon is revisited several times for every
    one pass over the far horizon.
    """
    today = today or dt.date.today()
    cursors = dict(cursors or {})
    pairs = date_pairs(cfg, today)

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
    picked: List[Tuple[str, str]] = []
    for index, (band, bucket) in enumerate(zip(bands, buckets)):
        if not bucket:
            continue
        budget = max(1, int(round(cfg.source.pairs_per_run * band.share / total_share)))
        got, cursors[str(index)] = slice_for_run(
            bucket, int(cursors.get(str(index), 0)), budget
        )
        picked.extend(got)
    return picked, cursors
