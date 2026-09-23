"""Wide scan: price every departure date in a window with one request.

Google Flights' calendar view is served by an internal call, GetCalendarGraph,
that returns the cheapest round-trip price for every departure date in a window
of up to 61 days, for one trip length. The whole 14-105 day horizon for one
destination and one trip length is two requests. The per-pair backend needs 92.

It is a first pass, not the source of truth. The calendar says *this date is
cheap*; a full search (sources/pairs.py) says which flight, which airports and
what it really costs. Grid prices decide where full searches are spent. They
never fire an alert on their own.

The call is reverse-engineered by the `flights` package (punitarani/fli). It is
the most fragile thing in this project, so every failure here degrades to the
adaptive scheduler running on its own rather than stopping the sweep.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..config import Config

# GetCalendarGraph answers for at most this many departure dates per request.
WINDOW_DAYS = 61


class GridUnavailable(RuntimeError):
    """The calendar endpoint can't be used at all: library missing or broken."""


@dataclass(frozen=True)
class Combo:
    """One calendar scan: a destination, a trip length, a stop class."""

    destination: str
    nights: int
    nonstop: bool

    @property
    def key(self) -> str:
        return "%s|%d|%d" % (self.destination, self.nights, 0 if self.nonstop else 1)

    @classmethod
    def from_key(cls, key: str) -> "Combo":
        destination, nights, stops = key.split("|")
        return cls(destination, int(nights), stops == "0")


def windows(
    start: dt.date, end: dt.date, size: int = WINDOW_DAYS
) -> List[Tuple[dt.date, dt.date]]:
    """Split [start, end] into consecutive windows of at most `size` days."""
    out = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=size - 1), end)
        out.append((cursor, stop))
        cursor = stop + dt.timedelta(days=1)
    return out


def combos(cfg: Config) -> List[Combo]:
    """Every scan needed to price the whole search space once."""
    classes = [True] if cfg.search.max_stops == 0 else [True, False]
    return [
        Combo(destination, nights, nonstop)
        for destination in cfg.route.destinations
        for nights in sorted(cfg.search.trip_nights)
        for nonstop in classes
    ]


class GridScanner:
    """Calendar scans, minus the network call.

    Everything except `_fetch` is plain logic, so it can be tested without the
    `flights` package (which needs Python >= 3.10) or a network.
    """

    name = "grid"
    # Pause between requests. Only real network scanners need to be polite.
    paced = True

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.errors: List[str] = []
        self.requests = 0

    # -- configuration --------------------------------------------------------

    def origins(self) -> List[str]:
        banned = {a.upper() for a in self.cfg.route.exclude_origins}
        return [a for a in self.cfg.route.origin_airports if a.upper() not in banned]

    def horizon(self, today: Optional[dt.date] = None) -> Tuple[dt.date, dt.date]:
        today = today or dt.date.today()
        start = today + dt.timedelta(days=self.cfg.search.min_days_ahead)
        return start, start + dt.timedelta(days=self.cfg.search.window_days)

    # -- scanning -------------------------------------------------------------

    def scan(
        self, combo: Combo, today: Optional[dt.date] = None
    ) -> Optional[Dict[str, float]]:
        """{departure date: cheapest price} across the horizon, or None on failure.

        A window that fails is recorded and skipped. The scan as a whole counts
        as failed only when every window failed, since half a calendar is still
        useful for deciding where to look.
        """
        start, end = self.horizon(today)
        prices: Dict[str, float] = {}
        failures = 0
        spans = windows(start, end)
        lo, hi = (list(self.cfg.grid.jitter_seconds) + [0, 0])[:2]

        for index, (first, last) in enumerate(spans):
            if self.paced and (index or self.requests):
                time.sleep(random.uniform(float(lo), float(hi)))
            self.requests += 1
            try:
                rows = self._fetch(combo, first, last)
            except GridUnavailable:
                raise
            except Exception as exc:  # reverse-engineered endpoint; fails creatively
                failures += 1
                self.errors.append(
                    "grid %s %s..%s: %s: %s"
                    % (combo.key, first, last, type(exc).__name__, exc)
                )
                continue
            for out_date, ret_date, price in rows or []:
                try:
                    out_day = dt.date.fromisoformat(out_date)
                    nights = (dt.date.fromisoformat(ret_date) - out_day).days
                except ValueError:
                    continue
                # The endpoint pads its answer with neighbouring dates; keep
                # only what was asked for, at the trip length that was asked.
                if nights != combo.nights or not (start <= out_day <= end):
                    continue
                if price and price > 0:
                    prices[out_date] = min(price, prices.get(out_date, price))

        if failures == len(spans):
            return None
        return prices

    def _fetch(
        self, combo: Combo, first: dt.date, last: dt.date
    ) -> List[Tuple[str, str, float]]:
        """(departure, return, price) rows for one window. Subclasses implement."""
        raise NotImplementedError


class FliGridScanner(GridScanner):
    """GetCalendarGraph via the `flights` package."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        try:
            from fli.core import build_date_search_segments
            from fli.models import (
                Airport,
                BagsFilter,
                DateSearchFilters,
                MaxStops,
                PassengerInfo,
                TripType,
            )
            from fli.search import SearchDates
        except ImportError as exc:
            raise GridUnavailable(
                "the `flights` package is not importable (needs Python >= 3.10): %s"
                % exc
            ) from exc

        self._build_segments = build_date_search_segments
        self._Airport = Airport
        self._BagsFilter = BagsFilter
        self._DateSearchFilters = DateSearchFilters
        self._MaxStops = MaxStops
        self._PassengerInfo = PassengerInfo
        self._TripType = TripType
        self._search = SearchDates()

    def _airport(self, code: str):
        try:
            return self._Airport[code.upper()]
        except KeyError as exc:
            raise GridUnavailable("unknown airport code %r" % code) from exc

    def _fetch(
        self, combo: Combo, first: dt.date, last: dt.date
    ) -> List[Tuple[str, str, float]]:
        s = self.cfg.search
        segments, trip_type = self._build_segments(
            origin=[self._airport(code) for code in self.origins()],
            destination=self._airport(combo.destination),
            start_date=first.isoformat(),
            trip_duration=combo.nights,
            is_round_trip=True,
        )
        filters = self._DateSearchFilters(
            trip_type=trip_type,
            passenger_info=self._PassengerInfo(adults=s.adults),
            flight_segments=segments,
            stops=(
                self._MaxStops.NON_STOP
                if combo.nonstop
                else self._MaxStops.ONE_STOP_OR_FEWER
            ),
            bags=(
                self._BagsFilter(
                    checked_bags=s.checked_bags, carry_on=bool(s.carry_on_bags)
                )
                if self.cfg.grid.include_bags
                else None
            ),
            from_date=first.isoformat(),
            to_date=last.isoformat(),
            duration=combo.nights,
        )
        # One window per call keeps the library from fanning out in parallel
        # threads; pacing between calls is ours to control.
        results = self._search.search(filters, currency=s.currency) or []
        rows = []
        for item in results:
            dates = getattr(item, "date", ()) or ()
            if len(dates) != 2:
                continue
            rows.append(
                (
                    dates[0].date().isoformat(),
                    dates[1].date().isoformat(),
                    float(item.price),
                )
            )
        return rows


class MockGridScanner(GridScanner):
    """Deterministic calendar prices, for running the pipeline offline."""

    paced = False

    def _fetch(
        self, combo: Combo, first: dt.date, last: dt.date
    ) -> List[Tuple[str, str, float]]:
        import hashlib
        import os

        seed = os.getenv("FT_MOCK_SEED", "0")
        rows = []
        day = first
        while day <= last:
            digest = hashlib.sha256(
                ("%s|%s|%s" % (seed, combo.key, day)).encode()
            ).hexdigest()
            price = 240 + 180 * (int(digest[:8], 16) / 0xFFFFFFFF)
            if not combo.nonstop:
                price *= 0.8
            rows.append(
                (
                    day.isoformat(),
                    (day + dt.timedelta(days=combo.nights)).isoformat(),
                    round(price),
                )
            )
            day += dt.timedelta(days=1)
        return rows


def get_grid(cfg: Config) -> Optional[GridScanner]:
    """The configured calendar scanner, or None when the grid is switched off."""
    if not cfg.grid.enabled:
        return None
    if cfg.source.backend == "mock":
        return MockGridScanner(cfg)
    return FliGridScanner(cfg)
