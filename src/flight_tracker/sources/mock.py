"""Deterministic fake fares, so the whole pipeline can be tested offline.

Prices are a function of the date pair, with a seeded wobble and a couple of
planted bargains, so `run --backend mock` exercises alerting end to end.
"""

from __future__ import annotations

import hashlib
import os
from typing import List

from ..config import Config
from .base import Item, Offer, Source

AIRLINES = [["JetBlue"], ["Delta"], ["American"], ["United"], ["Alaska"]]


def _hash_float(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


class MockSource(Source):
    name = "mock"

    def drill(self, items: List[Item]) -> List[Offer]:
        if os.getenv("FT_MOCK_EMPTY"):  # for exercising the dead man's switch
            return []

        seed = os.getenv("FT_MOCK_SEED", "0")

        offers: List[Offer] = []
        for out_date, ret_date, dest in items:
            r = _hash_float(seed, out_date, ret_date, dest)
            price = 240 + 180 * r
            if _hash_float("deal", seed, out_date) < 0.08:
                price *= 0.55  # planted bargain
            offers.append(
                Offer(
                    out_date=out_date,
                    ret_date=ret_date,
                    price=round(price),
                    currency=self.cfg.search.currency,
                    airlines=AIRLINES[int(r * len(AIRLINES)) % len(AIRLINES)],
                    url="https://www.google.com/travel/flights",
                    stops=0,
                    duration_minutes=355,
                    dep_airport="JFK",
                    arr_airport=dest,
                    flight_no="B6 %d" % (100 + int(r * 800)),
                )
            )
            if self.cfg.search.max_stops >= 1:
                # A connecting fare, usually cheaper and occasionally a steal.
                offers.append(
                    Offer(
                        out_date=out_date,
                        ret_date=ret_date,
                        price=round(price * (0.55 + 0.3 * r)),
                        currency=self.cfg.search.currency,
                        airlines=AIRLINES[int(r * 7) % len(AIRLINES)],
                        url="https://www.google.com/travel/flights",
                        stops=1,
                        duration_minutes=480,
                        dep_airport="LGA",
                        arr_airport=dest,
                        flight_no="WN %d / %d" % (1000 + int(r * 900), 2000 + int(r * 700)),
                    )
                )
        return offers
