"""Calendar-grid backend (experimental, opt-in).

The original design for this tracker assumed Google's price-calendar matrix
could be pulled in ONE request via `fast_flights.get_calendar_grid`. That
function does not exist in upstream fast-flights 3.1.0 -- it only lives in
community forks of a much older version.

This module is the seam for it. If you vet a fork that provides
`get_calendar_grid`, install it and set `source.backend: grid`. If the symbol
isn't there, we fail loudly at construction rather than silently returning no
data (silent no-data is exactly what the dead man's switch exists to catch,
and we shouldn't make it do this job).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Tuple

from ..config import Config
from .base import Offer, ScrapeError, Source


class GridSource(Source):
    name = "grid"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        try:
            from fast_flights import get_calendar_grid  # type: ignore[attr-defined]
        except (ImportError, AttributeError) as exc:
            raise ScrapeError(
                "source.backend='grid' needs a fast-flights build that provides "
                "get_calendar_grid; the published 3.1.0 does not. Use "
                "backend='pairs', or install a fork you have reviewed."
            ) from exc
        self._get_calendar_grid = get_calendar_grid

    def sweep(self, cursor: int = 0) -> Tuple[List[Offer], int]:
        s = self.cfg.search
        start = dt.date.today() + dt.timedelta(days=s.min_days_ahead)
        end = start + dt.timedelta(days=s.window_days)

        calendar: Any = self._get_calendar_grid(
            from_city=self.cfg.route.origin,
            to_city=self.cfg.route.destination,
            departure_range=(start.isoformat(), end.isoformat()),
            return_range=(start.isoformat(), end.isoformat()),
            max_stops=s.max_stops,
            bags=(s.checked_bags, s.carry_on_bags),
            currency=s.currency,
        )

        offers: List[Offer] = []
        for entry in getattr(calendar, "entries", calendar) or []:
            price = getattr(entry, "price", None)
            out_date = getattr(entry, "outbound_date", None)
            ret_date = getattr(entry, "return_date", None)
            if not (price and out_date and ret_date):
                continue
            nights = (
                dt.date.fromisoformat(str(ret_date))
                - dt.date.fromisoformat(str(out_date))
            ).days
            if nights not in s.trip_nights:
                continue
            offers.append(
                Offer(
                    out_date=str(out_date),
                    ret_date=str(ret_date),
                    price=float(price),
                    currency=getattr(entry, "currency", s.currency),
                )
            )
        return offers, cursor
