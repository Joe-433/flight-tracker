"""Date-pair backend: one Google Flights query per (outbound, return) pair.

This is the backend that works against `fast-flights` as actually published
(3.1.0). The full date space is ~45 pairs, which is too many to sweep every
run, so each run walks a rotating slice of it (`source.pairs_per_run`) and
stores a cursor in state. Full coverage takes a few runs; that's the trade we
make for not hammering Google.

City MIDs (e.g. "/m/02_286") are passed straight through as the airport field,
which is how one query covers every airport in the metro.
"""

from __future__ import annotations

import random
import time
from typing import Any, List, Optional, Tuple

from ..config import Config
from .base import Offer, ScrapeError, Source, date_pairs, slice_for_run


class PairsSource(Source):
    name = "pairs"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        try:
            from fast_flights import (  # noqa: F401
                FlightQuery,
                Passengers,
                create_query,
                get_flights,
            )
        except ImportError as exc:  # pragma: no cover - environment problem
            # Two very different causes, so name them separately: a missing
            # fast_flights means it isn't installed (it needs Python >= 3.10);
            # a missing anything-else means its dependency tree is incomplete,
            # which it genuinely is -- see requirements.txt.
            missing = getattr(exc, "name", "") or str(exc)
            if "fast_flights" in missing:
                detail = "fast-flights is not installed (it needs Python >= 3.10)"
            else:
                detail = (
                    "fast-flights is installed but its dependency %r is missing; "
                    "run `pip install -r requirements.txt`" % missing
                )
            raise ScrapeError("%s: %s" % (detail, exc)) from exc

        self._FlightQuery = FlightQuery
        self._Passengers = Passengers
        self._create_query = create_query
        self._get_flights = get_flights

    # -- query construction -------------------------------------------------

    def _query(self, out_date: str, ret_date: str) -> Any:
        s = self.cfg.search
        r = self.cfg.route
        return self._create_query(
            flights=[
                self._FlightQuery(
                    date=out_date, from_airport=r.origin, to_airport=r.destination
                ),
                self._FlightQuery(
                    date=ret_date, from_airport=r.destination, to_airport=r.origin
                ),
            ],
            trip="round-trip",
            seat=s.seat,
            passengers=self._Passengers(adults=s.adults),
            max_stops=s.max_stops,
            carry_on_bags=s.carry_on_bags,
            checked_bags=s.checked_bags,
            currency=s.currency,
            exclude_basic_economy=s.exclude_basic_economy,
        )

    # -- fetching -----------------------------------------------------------

    def fetch_pair(self, out_date: str, ret_date: str) -> Optional[Offer]:
        """Cheapest nonstop roundtrip for one date pair, or None."""
        query = self._query(out_date, ret_date)
        url = query.url()

        last_exc: Optional[Exception] = None
        for attempt in range(self.cfg.source.retries + 1):
            try:
                results = self._get_flights(query)
                break
            except Exception as exc:  # the scraper fails in many creative ways
                last_exc = exc
                if attempt < self.cfg.source.retries:
                    time.sleep(1.5 * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or exhausts
            results = None

        if last_exc is not None and not results:
            self.errors.append("%s->%s: %s" % (out_date, ret_date, last_exc))
            return None
        if not results:
            return None

        best: Optional[Offer] = None
        for item in results:
            offer = self._to_offer(item, out_date, ret_date, url)
            if offer is None:
                continue
            if best is None or offer.price < best.price:
                best = offer
        return best

    def _to_offer(
        self, item: Any, out_date: str, ret_date: str, url: str
    ) -> Optional[Offer]:
        price = getattr(item, "price", None)
        if not isinstance(price, (int, float)) or price <= 0:
            return None

        legs = list(getattr(item, "flights", []) or [])
        stops = max(0, len(legs) - 1) if legs else None

        # Belt and braces: the server-side nonstop filter is the primary
        # guard, but if Google ever ignores it we drop connections here too.
        if self.cfg.search.max_stops == 0 and stops not in (None, 0):
            return None

        duration = None
        if legs and all(getattr(f, "duration", None) for f in legs):
            duration = sum(int(f.duration) for f in legs)

        return Offer(
            out_date=out_date,
            ret_date=ret_date,
            price=float(price),
            currency=self.cfg.search.currency,
            airlines=list(getattr(item, "airlines", []) or []),
            url=url,
            stops=stops,
            duration_minutes=duration,
        )

    # -- sweep --------------------------------------------------------------

    def sweep(self, cursor: int = 0) -> Tuple[List[Offer], int]:
        pairs = date_pairs(self.cfg)
        picked, next_cursor = slice_for_run(
            pairs, cursor, self.cfg.source.pairs_per_run
        )

        lo, hi = (self.cfg.source.jitter_seconds + [0, 0])[:2]
        offers: List[Offer] = []
        for i, (out_date, ret_date) in enumerate(picked):
            if i:
                time.sleep(random.uniform(float(lo), float(hi)))
            offer = self.fetch_pair(out_date, ret_date)
            if offer is not None:
                offers.append(offer)

        return offers, next_cursor
