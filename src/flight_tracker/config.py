"""Config loading. One YAML file, dataclasses, no magic."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Route:
    # Query token for full searches. The NY city MID covers JFK, LGA and EWR
    # in one request; unwanted origins are filtered out of the results.
    origin: str
    # Destination airports, each searched by name. The LA city MID returns LAX
    # and nothing else (verified 49/49), so the basin has to be spelled out.
    destinations: List[str] = field(default_factory=list)
    # The airports `origin` covers. The calendar grid can't take a MID, so it
    # is given these explicitly, minus the exclusions.
    origin_airports: List[str] = field(default_factory=list)
    # Origin airports to drop even though the city MID returns them.
    exclude_origins: List[str] = field(default_factory=list)
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
class Source:
    backend: str = "pairs"
    # Full searches per run, split evenly across shards. Each shard is its own
    # GitHub runner with its own IP.
    drills_per_run: int = 60
    shards: int = 4
    jitter_seconds: List[float] = field(default_factory=lambda: [1, 3])
    retries: int = 2


@dataclass
class Grid:
    """Calendar scans: every departure date priced in one request per window."""

    enabled: bool = True
    # Rescan a (destination, trip length, stop class) at most this often. With
    # an external trigger every 30 minutes, 25 means "every run".
    refresh_minutes: float = 25.0
    jitter_seconds: List[float] = field(default_factory=lambda: [1, 2])
    # Consecutive failed runs before sending a "grid is down" notice. The
    # sweep carries on without it, so this is a warning, not the dead man.
    notify_after_failures: int = 3
    # Ask the calendar to fold carry-on/checked bag fees into its prices. Off,
    # because full searches can't: fast-flights sends the same bag filter and
    # it has no measurable effect on the prices it returns. With bags on, the
    # calendar ran a median $90 above full searches on the same trips (0 of 74
    # exact); with bags off, 19 of 74 exact and 34 within $15. The calendar is
    # only useful for steering full searches if both price the same way.
    include_bags: bool = False


@dataclass
class Schedule:
    """How often a date pair earns a full search. See planner.py."""

    base_hours: float = 24.0      # unremarkable fares
    cheap_hours: float = 3.0      # bottom 20% of their stop class
    cheapest_hours: float = 1.0   # bottom 5%, or the calendar says it moved
    urgent_hours: float = 0.25    # under an alert threshold, or a new low
    moved_usd: float = 10.0       # calendar this far under the last full search


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
    stale_hours: float = 36.0      # drop unrefreshed fares from the reports


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
    grid: Grid = field(default_factory=Grid)
    schedule: Schedule = field(default_factory=Schedule)
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
        grid=_build(Grid, raw.get("grid")),
        schedule=_build(Schedule, raw.get("schedule")),
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
    if os.getenv("FT_DRILLS_PER_RUN"):
        cfg.source.drills_per_run = int(os.environ["FT_DRILLS_PER_RUN"])
    if os.getenv("FT_GRID") in ("0", "false", "off"):
        cfg.grid.enabled = False

    if not cfg.route.destinations:
        raise ValueError("route.destinations must list at least one airport")

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
