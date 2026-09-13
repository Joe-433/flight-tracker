"""Shared types for scraping backends."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..config import Config


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

    @property
    def key(self) -> str:
        return "%s|%s" % (self.out_date, self.ret_date)

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

    def sweep(self, cursor: int = 0) -> Tuple[List[Offer], int]:
        """Return (offers, next_cursor).

        `cursor` lets a backend cover a slice of the search space per run and
        resume where it left off. Backends that cover everything in one shot
        just return the cursor unchanged.
        """
        raise NotImplementedError


def date_pairs(cfg: Config, today: Optional[dt.date] = None) -> List[Tuple[str, str]]:
    """Every (outbound, return) pair inside the rolling window.

    The window is `window_days` long starting `min_days_ahead` from today, and
    the return leg must also land inside it.
    """
    today = today or dt.date.today()
    start = today + dt.timedelta(days=cfg.search.min_days_ahead)
    end = start + dt.timedelta(days=cfg.search.window_days)

    pairs: List[Tuple[str, str]] = []
    for offset in range(cfg.search.window_days + 1):
        out = start + dt.timedelta(days=offset)
        for nights in sorted(cfg.search.trip_nights):
            ret = out + dt.timedelta(days=nights)
            if ret > end:
                continue
            pairs.append((out.isoformat(), ret.isoformat()))
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
