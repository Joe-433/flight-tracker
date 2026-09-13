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
    window_days: int = 91   # span of DEPARTURE dates, starting min_days_ahead out
    min_days_ahead: int = 14
    trip_nights: List[int] = field(default_factory=lambda: [5, 6, 7])
    max_stops: int = 1
    carry_on_bags: int = 1
    checked_bags: int = 0
    currency: str = "USD"
    adults: int = 1
    seat: str = "economy"
    exclude_basic_economy: bool = False


@dataclass
class Band:
    """A slice of the horizon that gets its own share of the request budget.

    Fares 3 months out barely move day to day; fares 10 days out move fast.
    Sweeping both at the same rate wastes requests on the far end and starves
    the near end, so each band rotates on its own cursor.
    """

    within_days: int
    share: float = 1.0


@dataclass
class Source:
    backend: str = "pairs"
    pairs_per_run: int = 24
    jitter_seconds: List[float] = field(default_factory=lambda: [2, 5])
    retries: int = 2
    bands: List[Band] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bands = [b if isinstance(b, Band) else Band(**b) for b in self.bands]
        self.bands.sort(key=lambda b: b.within_days)


@dataclass
class Alerts:
    threshold_usd: float = 250.0
    threshold_usd_with_stops: float = 200.0  # a layover has to be worth it
    cooldown_hours: float = 12.0
    rebeat_drop_usd: float = 15.0
    max_per_run: int = 3
    record_low: bool = True
    record_min_drop_usd: float = 5.0


@dataclass
class History:
    days: int = 30
    max_points_per_pair: int = 40  # hard cap; state.json is committed every run
    resample_hours: float = 12.0   # log an unchanged price at most this often
    min_change_usd: float = 5.0    # ignore noise smaller than this
    stale_hours: float = 24.0      # drop unrefreshed fares from the reports


@dataclass
class Deals:
    min_observations: int = 6
    pct_below_baseline: float = 0.15
    cheap_percentile: float = 0.05


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
    history: History = field(default_factory=History)
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
        history=_build(History, raw.get("history")),
        deals=_build(Deals, raw.get("deals")),
        deadman=_build(Deadman, raw.get("deadman")),
    )

    # Env overrides, so a GitHub Actions run can be retuned without a commit.
    if os.getenv("FT_THRESHOLD_USD"):
        cfg.alerts.threshold_usd = float(os.environ["FT_THRESHOLD_USD"])
    if os.getenv("FT_BACKEND"):
        cfg.source.backend = os.environ["FT_BACKEND"]
    if os.getenv("FT_PAIRS_PER_RUN"):
        cfg.source.pairs_per_run = int(os.environ["FT_PAIRS_PER_RUN"])

    if cfg.search.max_stops != 0 and (
        cfg.alerts.threshold_usd_with_stops >= cfg.alerts.threshold_usd
    ):
        # Connections are allowed only because they might be much cheaper. If
        # their threshold isn't lower, they'll just crowd out nonstop alerts.
        print(
            "warning: connections are allowed but their threshold (%s) is not "
            "below the nonstop threshold (%s)"
            % (cfg.alerts.threshold_usd_with_stops, cfg.alerts.threshold_usd)
        )
    return cfg
