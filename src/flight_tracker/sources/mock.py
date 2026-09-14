"""Deterministic fake fares, so the whole pipeline can be tested offline.

Prices are a function of the date pair, with a seeded wobble and a couple of
planted bargains, so `run --backend mock` exercises alerting end to end.
"""

from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional, Tuple

from ..config import Config
from .base import Offer, Source, plan_slice

AIRLINES = [["JetBlue"], ["Delta"], ["American"], ["United"], ["Alaska"]]


def _hash_float(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


class MockSource(Source):
    name = "mock"

    def sweep(
        self, cursors: Optional[Dict[str, int]] = None
    ) -> Tuple[List[Offer], Dict[str, int]]:
        if os.getenv("FT_MOCK_EMPTY"):  # for exercising the dead man's switch
            return [], dict(cursors or {})

        seed = os.getenv("FT_MOCK_SEED", "0")
        picked, next_cursors = plan_slice(self.cfg, cursors)

        offers: List[Offer] = []
        for out_date, ret_date, _dest in picked:
            r = _hash_float(seed, out_date, ret_date)
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
                    )
                )
        return offers, next_cursors
