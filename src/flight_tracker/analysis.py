"""Turning prices into decisions: which fares are actually cheap?

Three independent signals, deliberately kept separate so you can see WHY
something fired:

  threshold  -- absolute: at or under the price you said you'd pay.
  baseline   -- relative to itself: this date pair, versus its own recent
                median. Catches a real drop on an expensive week.
  percentile -- relative to the route: bottom N% of everything we've logged
                lately. Catches "this specific day is just a cheap day".

ONLY `threshold` sends an alert. `percentile` and `baseline` are computed and
recorded either way -- they mark cheap days in the `days` report and explain
*why* an alerting fare is good -- but on their own they stay silent. A relative
bargain is still whatever the route happens to cost that week; you asked to
hear about $250, not about the best of a bad month.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .sources.base import Offer
from .state import State, hours_since, utcnow

def min_route_samples(cheap_percentile: float) -> int:
    """How much history a percentile cutoff needs before it means anything.

    A 5% cutoff computed from 20 observations is just "the single cheapest
    thing we've seen", which would alert on every new low. Requiring ~3
    observations below the cutoff makes it a real threshold: 60 samples for 5%,
    20 for 20%.
    """
    if cheap_percentile <= 0:
        return 20
    return max(20, int(math.ceil(3.0 / cheap_percentile)))


def threshold_for(offer, cfg) -> float:
    """The price bar this offer has to clear.

    A layover only earns its place by being materially cheaper, so connecting
    fares are held to a lower number than nonstops. Unknown stop count is
    treated as connecting: better to stay quiet than to alert on a fare that
    turns out to have a stop in it.
    """
    if offer.stops == 0:
        return cfg.alerts.threshold_usd
    return cfg.alerts.threshold_usd_with_stops


def median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def percentile(values: List[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile. `q` in [0, 1]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


@dataclass
class Deal:
    offer: Offer
    reasons: List[str] = field(default_factory=list)
    baseline: Optional[float] = None
    discount: Optional[float] = None  # fraction below baseline, 0.22 = 22% off

    @property
    def alertworthy(self) -> bool:
        return "threshold" in self.reasons

    @property
    def score(self) -> float:
        """Lower is better; used only for ranking within one run."""
        return self.offer.price - 50.0 * len(self.reasons)


@dataclass
class DayStat:
    """Cheapest fare seen for a given outbound date, across all trip lengths."""

    out_date: str
    price: float
    ret_date: str
    cheap: bool = False
    samples: int = 0
    stops: int = 0


@dataclass
class RecordLow:
    """A fare that beat the cheapest ever seen in its stop class."""

    offer: Offer
    previous: float
    previous_seen: Optional[str] = None

    @property
    def saving(self) -> float:
        return self.previous - self.offer.price


def stop_class(offer: Offer) -> str:
    return "nonstop" if offer.stops == 0 else "connecting"


def claim_record_lows(
    offers: List[Offer],
    state: State,
    cfg: Config,
    now: Optional[dt.datetime] = None,
) -> List[RecordLow]:
    """Find fares beating the all-time low, and raise the bar. Mutates state.

    This exists because a fixed threshold can stay silent for months: if
    nothing on the route has ever been under $250, a $250 alarm never rings and
    the tracker looks dead even while it's working perfectly. A record low is
    self-limiting by construction -- every alert raises its own bar -- so it
    can never become a stream.

    The first fare in a class arms the bar silently. Alerting on it would mean
    alerting on literally the first thing we ever saw.
    """
    if not cfg.alerts.record_low:
        return []

    now = now or utcnow()
    best: Dict[str, Offer] = {}
    for offer in offers:
        name = stop_class(offer)
        if name not in best or offer.price < best[name].price:
            best[name] = offer

    found: List[RecordLow] = []
    for name, offer in best.items():
        record = state.records.get(name)

        # Let a record expire with the rest of the history, so the bar resets
        # with the season instead of being set forever by one winter fluke.
        if record is not None:
            age = hours_since(str(record.get("seen")), now)
            if age is not None and age > cfg.history.days * 24:
                record = None

        if record is None:
            state.records[name] = {"price": offer.price, "seen": now.isoformat()}
            continue

        previous = float(record.get("price", 0) or 0)
        if offer.price <= previous - cfg.alerts.record_min_drop_usd:
            found.append(
                RecordLow(
                    offer=offer,
                    previous=previous,
                    previous_seen=str(record.get("seen") or "") or None,
                )
            )
            state.records[name] = {"price": offer.price, "seen": now.isoformat()}

    return found


@dataclass
class Assessment:
    deals: List[Deal] = field(default_factory=list)
    cheapest: Optional[Offer] = None
    day_stats: List[DayStat] = field(default_factory=list)
    route_median: Optional[float] = None
    cheap_cutoff: Optional[float] = None

    @property
    def alerts(self) -> List[Deal]:
        return [d for d in self.deals if d.alertworthy]


def assess(
    offers: List[Offer],
    state: State,
    cfg: Config,
    now: Optional[dt.datetime] = None,
) -> Assessment:
    """Score this run's offers against stored history."""
    history = state.all_prices()
    route_median = median(history)
    needed = min_route_samples(cfg.deals.cheap_percentile)
    cheap_cutoff = (
        percentile(history, cfg.deals.cheap_percentile)
        if len(history) >= needed
        else None
    )

    result = Assessment(route_median=route_median, cheap_cutoff=cheap_cutoff)

    for offer in offers:
        reasons: List[str] = []
        baseline = None
        discount = None

        if offer.price <= threshold_for(offer, cfg):
            reasons.append("threshold")

        # `assess` runs before this sweep is recorded, so the series holds
        # only prior observations -- the fare is never part of its own baseline.
        prior = [p for _, p in state.series(offer.key)]
        if len(prior) >= cfg.deals.min_observations:
            baseline = median(prior)
            if baseline:
                discount = (baseline - offer.price) / baseline
                if discount >= cfg.deals.pct_below_baseline:
                    reasons.append("baseline")

        if cheap_cutoff is not None and offer.price <= cheap_cutoff:
            reasons.append("percentile")

        if reasons:
            result.deals.append(
                Deal(offer=offer, reasons=reasons, baseline=baseline, discount=discount)
            )

        if result.cheapest is None or offer.price < result.cheapest.price:
            result.cheapest = offer

    result.deals.sort(key=lambda d: d.score)
    result.day_stats = day_stats(offers, state, cheap_cutoff, now=now)
    return result


def day_stats(
    offers: List[Offer],
    state: State,
    cheap_cutoff: Optional[float],
    now: Optional[dt.datetime] = None,
) -> List[DayStat]:
    """Cheapest known fare per departure day -- the "low price days" view.

    Merges this run's fresh offers with stored history, so a rotating partial
    sweep still produces a full picture of the window.
    """
    today = (now or dt.datetime.now(dt.timezone.utc)).date().isoformat()
    best: Dict[str, DayStat] = {}

    def consider(out_date: str, ret_date: str, price: float, stops: int = 0) -> None:
        if out_date < today:
            return
        current = best.get(out_date)
        if current is None:
            best[out_date] = DayStat(
                out_date=out_date, price=price, ret_date=ret_date, samples=1,
                stops=stops,
            )
            return
        current.samples += 1
        if price < current.price:
            current.price = price
            current.ret_date = ret_date
            current.stops = stops

    for key, points in state.observations.items():
        parts = key.split("|")
        out_date, ret_date = parts[0], parts[1]
        stops = int(parts[2]) if len(parts) > 2 else 0
        for _, price in points:
            consider(out_date, ret_date, float(price), stops)

    for offer in offers:  # fresh data wins ties by being applied last
        consider(offer.out_date, offer.ret_date, offer.price, offer.stops or 0)

    stats = sorted(best.values(), key=lambda s: s.price)
    if cheap_cutoff is not None:
        for stat in stats:
            stat.cheap = stat.price <= cheap_cutoff
    return stats
