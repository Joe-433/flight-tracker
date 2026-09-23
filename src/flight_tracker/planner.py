"""Adaptive scheduling: which date pairs earn a full search this run.

Every (outbound, return, destination) combination is an *item*: 92 departure
dates x 3 trip lengths x 5 airports = 1,380 of them. A run affords a few dozen
full searches. Rotating through everything took about a day per cycle, and
spent most of that budget re-confirming fares that weren't going anywhere.

Instead, each item has a due interval set by what we know about it, and the
most overdue items go first:

    under an alert threshold, or a new record low       every 15 min
    cheapest 5% of its stop class, or the price moved   every 2 hours
    cheapest 20%                                        every 6 hours
    everything else                                     every 24 hours

The cheap tiers are deliberately not tighter. The calendar re-prices every
item on every run, and anything that falls under an alert line jumps to the
15-minute tier regardless, so exact searches on merely-cheap items refresh
details rather than catch deals.

"What we know" comes from two sources. The calendar grid prices every item on
every run, approximately. The last full search is exact, but it's only as
fresh as the last time that item won a slot.

The two are never compared directly: the calendar runs a median $17 below full
searches, so "calendar under the last full search" would be true of dozens of
items permanently, and they'd be re-searched every hour forever. Instead each
full search records what the calendar said at that moment, and movement is the
calendar now versus the calendar then -- same source, same bias, so the bias
cancels. The best estimate of an item's price is its last full search, shifted
by however far the calendar has moved since.

Nothing starves. An unremarkable item's overdue ratio (time since its last
search / its interval) keeps growing until it outranks cheap items that were
just checked. It waits longer, that's all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import Config
from .sources.base import Item, date_pairs
from .state import State, hours_since, parse_ts, utcnow

# Grid prices older than this are ignored rather than trusted.
GRID_MAX_AGE_HOURS = 6.0

# stop class -> {item key -> price}
Known = Dict[int, Dict[str, float]]


@dataclass(frozen=True)
class Candidate:
    item: Item
    overdue: float
    price: Optional[float]
    reason: str

    @property
    def key(self) -> str:
        return item_key(self.item)


def item_key(item: Item) -> str:
    return "%s|%s|%s" % item


def universe(cfg: Config, today: Optional[dt.date] = None) -> List[Item]:
    """Every item in the search space."""
    return [
        (out_date, ret_date, destination)
        for out_date, ret_date in date_pairs(cfg, today)
        for destination in cfg.route.destinations
    ]


def grid_view(
    state: State,
    fresh: Optional[Dict[str, Dict[str, float]]] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Dict[str, float]]:
    """Calendar prices worth trusting: this run's scans, plus recent stored ones."""
    now = now or utcnow()
    view: Dict[str, Dict[str, float]] = {}
    for combo_key, entry in state.grid.items():
        age = hours_since(str(entry.get("seen") or ""), now)
        if age is not None and age <= GRID_MAX_AGE_HOURS:
            view[combo_key] = dict(entry.get("prices") or {})
    for combo_key, prices in (fresh or {}).items():
        view[combo_key] = dict(prices)
    return view


def _grid_price(
    view: Dict[str, Dict[str, float]], item: Item, stops: int
) -> Optional[float]:
    out_date, ret_date, destination = item
    try:
        nights = (dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)).days
    except ValueError:
        return None
    prices = view.get("%s|%d|%d" % (destination, nights, stops))
    if not prices:
        return None
    value = prices.get(out_date)
    return float(value) if value else None


def _drill(
    state: State, item: Item, stops: int
) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """(price, seen, calendar price at the time) from the last full search."""
    snap = state.latest.get("%s|%d" % (item_key(item), stops))
    if not snap:
        return None, None, None
    try:
        price = float(snap["price"])
    except (KeyError, TypeError, ValueError):
        return None, None, None
    grid_at = snap.get("grid_at")
    return (
        price,
        str(snap.get("seen") or "") or None,
        float(grid_at) if grid_at not in (None, "") else None,
    )


def estimate(
    grid_now: Optional[float],
    drill_price: Optional[float],
    grid_at_drill: Optional[float],
) -> Tuple[Optional[float], float]:
    """(best price estimate, how far the calendar fell since the full search).

    With a full search and calendar readings from both then and now, shift the
    exact price by the calendar's movement. Without a full search, the raw
    calendar is all there is. Without a calendar baseline, the full search is.
    """
    if drill_price is not None and grid_now is not None and grid_at_drill is not None:
        fall = grid_at_drill - grid_now
        return drill_price - fall, fall
    if drill_price is not None:
        return drill_price, 0.0
    return grid_now, 0.0


def _percentiles(values: Dict[str, float]) -> Dict[str, float]:
    """Rank of each value within its set, 0.0 = cheapest, 1.0 = dearest."""
    ordered = sorted(values, key=values.get)
    span = max(len(ordered) - 1, 1)
    return {key: index / span for index, key in enumerate(ordered)}


def plan(
    cfg: Config,
    state: State,
    budget: Optional[int] = None,
    grid: Optional[Dict[str, Dict[str, float]]] = None,
    now: Optional[dt.datetime] = None,
) -> List[Candidate]:
    """The `budget` most overdue items, most urgent first."""
    now = now or utcnow()
    budget = cfg.source.drills_per_run if budget is None else budget
    sched = cfg.schedule
    view = grid_view(state, grid, now)
    items = universe(cfg, now.date())

    classes = [0] if cfg.search.max_stops == 0 else [0, 1]
    thresholds = {0: cfg.alerts.threshold_usd, 1: cfg.alerts.threshold_usd_with_stops}
    records = {
        0: (state.records.get("nonstop") or {}).get("price"),
        1: (state.records.get("connecting") or {}).get("price"),
    }

    known: Known = {stops: {} for stops in classes}
    moved: Dict[str, float] = {}
    for item in items:
        key = item_key(item)
        for stops in classes:
            drill_price, _, grid_at = _drill(state, item, stops)
            price, fall = estimate(_grid_price(view, item, stops), drill_price, grid_at)
            if price is not None:
                known[stops][key] = price
            if fall >= sched.moved_usd:
                moved[key] = max(fall, moved.get(key, 0.0))

    ranks = {stops: _percentiles(known[stops]) for stops in classes}

    candidates: List[Candidate] = []
    for item in items:
        key = item_key(item)
        prices = {s: known[s][key] for s in classes if key in known[s]}
        cheapest = min(prices.values()) if prices else None

        urgent = None
        for stops, price in prices.items():
            record = records.get(stops)
            if price <= thresholds[stops]:
                urgent = "%s $%.0f under the $%.0f alert line" % (
                    "nonstop" if stops == 0 else "one-stop", price, thresholds[stops]
                )
            elif record and price <= float(record) - cfg.alerts.record_min_drop_usd:
                urgent = "%s $%.0f beats the $%.0f record" % (
                    "nonstop" if stops == 0 else "one-stop", price, float(record)
                )
        rank = min(
            (ranks[s][key] for s in classes if key in ranks[s]), default=None
        )

        if urgent:
            interval, reason = sched.urgent_hours, urgent
        elif key in moved:
            interval, reason = sched.cheapest_hours, "calendar down $%.0f since last search" % moved[key]
        elif rank is not None and rank <= 0.05:
            interval, reason = sched.cheapest_hours, "cheapest 5%"
        elif rank is not None and rank <= 0.20:
            interval, reason = sched.cheap_hours, "cheapest 20%"
        else:
            interval, reason = sched.base_hours, "routine"

        age = _age_hours(state, item, now)
        # Never searched: due now, but not infinitely overdue -- otherwise a
        # fresh state would spend its first day on coverage and ignore the
        # calendar entirely.
        overdue = (age if age is not None else interval) / max(interval, 1e-6)
        candidates.append(Candidate(item, overdue, cheapest, reason))

    candidates.sort(
        key=lambda c: (-c.overdue, c.price if c.price is not None else float("inf"))
    )
    return candidates[: max(budget, 0)]


def _age_hours(state: State, item: Item, now: dt.datetime) -> Optional[float]:
    """Hours since this item's last full search, whatever it found."""
    stamp = state.checked.get(item_key(item))
    if stamp:
        return hours_since(stamp, now)
    # Before `checked` existed, the last successful search is the best proxy.
    seen = [_drill(state, item, stops)[1] for stops in (0, 1)]
    stamps = [parse_ts(s) for s in seen if s]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 3600.0


def partition(candidates: List[Candidate], shards: int) -> List[List[Item]]:
    """Deal items round-robin, so the urgent ones land on different runners."""
    shards = max(1, shards)
    out: List[List[Item]] = [[] for _ in range(shards)]
    for index, candidate in enumerate(candidates):
        out[index % shards].append(candidate.item)
    return out
