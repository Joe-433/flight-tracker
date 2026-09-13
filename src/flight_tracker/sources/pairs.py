"""Date-pair backend: one Google Flights query per (outbound, return) pair.

This is the backend that works against `fast-flights` as actually published
(3.1.0). The full date space is several hundred pairs, far too many to sweep every
run, so each run walks a rotating slice (`source.pairs_per_run`) split across
`source.bands` and stores one cursor per band in state. See `plan_slice`.

City MIDs (e.g. "/m/02_286") are passed straight through as the airport field,
which is how one query covers every airport in the metro.
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from .base import Offer, ScrapeError, Source, plan_slice


def _explain(exc: Exception) -> str:
    """Translate the scraper's failure modes into something actionable.

    `IndexError` here is not a bug in our code -- it's fast-flights' parser
    walking a Google payload that doesn't have the shape it expects, which in
    practice means Google returned nothing for that date pair. Distinguishing
    it from a genuine transport failure matters: a few of these per sweep is
    normal, all of them is the scraper breaking.
    """
    if isinstance(exc, (IndexError, KeyError, TypeError)):
        return "no parsable results (empty or unfamiliar Google payload): %r" % exc
    return "%s: %s" % (type(exc).__name__, exc)


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
        """Cheapest roundtrip for one date pair, or None."""
        query = self._query(out_date, ret_date)
        url = query.url()
        results = self._fetch(query, out_date, ret_date)
        if not results:
            return None

        best: Optional[Offer] = None
        for offer in self._offers(results, out_date, ret_date, url):
            if best is None or offer.price < best.price:
                best = offer
        return best

    def fetch_pair_by_stops(self, out_date: str, ret_date: str) -> List[Offer]:
        """Cheapest fare in each stop class for one date pair.

        Returning only the single cheapest would hide a nonstop that clears its
        own (higher) threshold whenever a connecting fare undercuts it without
        clearing the connecting threshold.
        """
        query = self._query(out_date, ret_date)
        url = query.url()
        results = self._fetch(query, out_date, ret_date)
        if not results:
            return []

        best: Dict[int, Offer] = {}
        for offer in self._offers(results, out_date, ret_date, url):
            stops = offer.stops if offer.stops is not None else 9
            if stops not in best or offer.price < best[stops].price:
                best[stops] = offer
        return [best[k] for k in sorted(best)]

    def _offers(
        self, results: Any, out_date: str, ret_date: str, url: str
    ) -> List[Offer]:
        offers = []
        for item in results:
            offer = self._to_offer(item, out_date, ret_date, url)
            if offer is not None:
                offers.append(offer)
        return offers

    def _fetch(self, query: Any, out_date: str, ret_date: str) -> Any:
        last_exc: Optional[Exception] = None
        results = None
        for attempt in range(self.cfg.source.retries + 1):
            try:
                results = self._get_flights(query)
                break
            except Exception as exc:  # the scraper fails in many creative ways
                last_exc = exc
                if attempt < self.cfg.source.retries:
                    time.sleep(1.5 * (attempt + 1))

        if last_exc is not None and not results:
            self.errors.append("%s->%s: %s" % (out_date, ret_date, _explain(last_exc)))
            return None
        return results

    def _to_offer(
        self, item: Any, out_date: str, ret_date: str, url: str
    ) -> Optional[Offer]:
        price = getattr(item, "price", None)
        if not isinstance(price, (int, float)) or price <= 0:
            return None

        legs = list(getattr(item, "flights", []) or [])
        stops = max(0, len(legs) - 1) if legs else None

        # Belt and braces: the server-side filter is the primary guard, but if
        # Google ever ignores it we enforce the stop limit here too. Note that
        # `legs` describes the OUTBOUND leg only -- Google returns outbound
        # options priced for the whole roundtrip -- so the return leg's stop
        # count is enforced server-side and not visible here.
        if stops is not None and stops > self.cfg.search.max_stops:
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

    def sweep(
        self, cursors: Optional[Dict[str, int]] = None
    ) -> Tuple[List[Offer], Dict[str, int]]:
        picked, next_cursors = plan_slice(self.cfg, cursors)

        lo, hi = (list(self.cfg.source.jitter_seconds) + [0, 0])[:2]
        offers: List[Offer] = []
        for i, (out_date, ret_date) in enumerate(picked):
            if i:
                time.sleep(random.uniform(float(lo), float(hi)))
            offers.extend(self.fetch_pair_by_stops(out_date, ret_date))

        return offers, next_cursors
