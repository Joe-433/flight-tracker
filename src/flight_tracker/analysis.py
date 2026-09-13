"""Turning prices into decisions: which fares are actually cheap?

Three independent signals, deliberately kept separate so you can see WHY
something fired:

  threshold  -- absolute: at or under the price you said you'd pay.
  baseline   -- relative to itself: this date pair, versus its own recent
                median. Catches a real drop on an expensive week.
  percentile -- relative to the route: bottom N% of everything we've logged
                lately. Catches "this specific day is just a cheap day".

A fare alerts on `threshold` alone, or on `percentile` alone. `baseline` never
alerts by itself -- a 15% drop from an absurd price is still an absurd price --
but it rides along in the "why" so you can see when a fare is both cheap in
absolute terms and falling.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .sources.base import Offer
from .state import State

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
        return "threshold" in self.reasons or "percentile" in self.reasons

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

        if offer.price <= cfg.alerts.threshold_usd:
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

    def consider(out_date: str, ret_date: str, price: float) -> None:
        if out_date < today:
            return
        current = best.get(out_date)
        if current is None:
            best[out_date] = DayStat(
                out_date=out_date, price=price, ret_date=ret_date, samples=1
            )
            return
        current.samples += 1
        if price < current.price:
            current.price = price
            current.ret_date = ret_date

    for key, points in state.observations.items():
        out_date, _, ret_date = key.partition("|")
        for _, price in points:
            consider(out_date, ret_date, float(price))

    for offer in offers:  # fresh data wins ties by being applied last
        consider(offer.out_date, offer.ret_date, offer.price)

    stats = sorted(best.values(), key=lambda s: s.price)
    if cheap_cutoff is not None:
        for stat in stats:
            stat.cheap = stat.price <= cheap_cutoff
    return stats
