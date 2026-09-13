"""Config loading. One YAML file, dataclasses, no magic."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Route:
    origin: str
    destination: str
    origin_label: str = "origin"
    destination_label: str = "destination"


@dataclass
class Search:
    window_days: int = 14
    min_days_ahead: int = 0
    trip_nights: List[int] = field(default_factory=lambda: [3, 4, 5, 6, 7])
    max_stops: int = 0
    carry_on_bags: int = 1
    checked_bags: int = 0
    currency: str = "USD"
    adults: int = 1
    seat: str = "economy"
    exclude_basic_economy: bool = False


@dataclass
class Source:
    backend: str = "pairs"
    pairs_per_run: int = 16
    jitter_seconds: List[float] = field(default_factory=lambda: [1, 3])
    retries: int = 2


@dataclass
class Alerts:
    threshold_usd: float = 250.0
    cooldown_hours: float = 12.0
    rebeat_drop_usd: float = 15.0
    max_per_run: int = 3


@dataclass
class Deals:
    history_days: int = 30
    min_observations: int = 6
    pct_below_baseline: float = 0.15
    cheap_percentile: float = 0.20


@dataclass
class Deadman:
    fail_runs: int = 2
    stale_hours: float = 6.0
    renotify_hours: float = 24.0


@dataclass
class Config:
    route: Route
    search: Search = field(default_factory=Search)
    source: Source = field(default_factory=Source)
    alerts: Alerts = field(default_factory=Alerts)
    deals: Deals = field(default_factory=Deals)
    deadman: Deadman = field(default_factory=Deadman)


def _build(cls, raw: Optional[Dict[str, Any]]):
    """Instantiate a config dataclass, ignoring unknown keys loudly."""
    raw = raw or {}
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            "unknown key(s) in %s config: %s" % (cls.__name__, ", ".join(sorted(unknown)))
        )
    return cls(**raw)


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if "route" not in raw:
        raise ValueError("config is missing the required `route:` section")

    cfg = Config(
        route=_build(Route, raw.get("route")),
        search=_build(Search, raw.get("search")),
        source=_build(Source, raw.get("source")),
        alerts=_build(Alerts, raw.get("alerts")),
        deals=_build(Deals, raw.get("deals")),
        deadman=_build(Deadman, raw.get("deadman")),
    )

    # Env overrides, so a GitHub Actions run can be retuned without a commit.
    if os.getenv("FT_THRESHOLD_USD"):
        cfg.alerts.threshold_usd = float(os.environ["FT_THRESHOLD_USD"])
    if os.getenv("FT_BACKEND"):
        cfg.source.backend = os.environ["FT_BACKEND"]

    if cfg.search.max_stops != 0:
        # Not an error, but the whole point of this tracker is nonstops.
        print("warning: max_stops != 0, alerts will include connections")
    return cfg
