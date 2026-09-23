"""Shared fixtures. Plain unittest, no extra dependencies."""

from __future__ import annotations

import datetime as dt
import sys
from os.path import abspath, dirname, join

sys.path.insert(0, join(dirname(dirname(abspath(__file__))), "src"))

from flight_tracker.config import (  # noqa: E402
    Alerts,
    Config,
    Deadman,
    Deals,
    Grid,
    Route,
    Schedule,
    Search,
    Source,
)
from flight_tracker.sources.base import Offer  # noqa: E402

NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.timezone.utc)


def make_config(**overrides) -> Config:
    cfg = Config(
        route=Route(
            origin="/m/02_286",
            destinations=["LAX"],
            origin_airports=["JFK", "LGA", "EWR"],
            exclude_origins=["EWR"],
        ),
        search=Search(),
        source=Source(),
        grid=Grid(jitter_seconds=[0, 0]),
        schedule=Schedule(),
        alerts=Alerts(),
        deals=Deals(),
        deadman=Deadman(),
    )
    for section, values in overrides.items():
        for key, value in values.items():
            setattr(getattr(cfg, section), key, value)
    return cfg


def make_offer(
    price: float, out_date: str = "2026-09-20", nights: int = 4, stops: int = 0
) -> Offer:
    ret = (dt.date.fromisoformat(out_date) + dt.timedelta(days=nights)).isoformat()
    return Offer(
        out_date=out_date, ret_date=ret, price=price, stops=stops,
        dep_airport="JFK", arr_airport="LAX",
    )
