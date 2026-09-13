"""Dead man's switch.

The real failure mode here is not a crash -- it's the scraper quietly
returning zero results forever while the workflow stays green and you assume
fares are just high. This module makes silence itself an alertable event.

Two independent triggers:
  * N consecutive runs produced no usable price, or
  * no usable price has been seen for H hours (catches runs that never fire
    at all -- GitHub disabling the cron, a workflow that won't start).

The second one only fires when a run does happen, so pair it with a heartbeat
if you want true "did the runner die" coverage (see README).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .state import State, hours_since, utcnow


@dataclass
class Health:
    down: bool = False
    recovered: bool = False
    should_notify: bool = False
    reasons: List[str] = field(default_factory=list)
    hours_stale: Optional[float] = None


def update(
    state: State,
    cfg: Config,
    got_data: bool,
    errors: Optional[List[str]] = None,
    now: Optional[dt.datetime] = None,
) -> Health:
    """Fold this run's outcome into the health counters. Mutates `state`."""
    now = now or utcnow()
    errors = errors or []
    health = Health()

    state.last_run = now.isoformat()

    if got_data:
        was_down = bool(state.deadman.get("down"))
        state.last_data = now.isoformat()
        state.consecutive_failures = 0
        state.deadman = {"down": False, "since": None, "last_notified": None}
        if was_down:
            health.recovered = True
            health.should_notify = True
        return health

    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    stale = hours_since(state.last_data, now)
    health.hours_stale = stale

    if state.consecutive_failures >= cfg.deadman.fail_runs:
        health.reasons.append(
            "%d consecutive sweeps returned no fares" % state.consecutive_failures
        )
    if stale is not None and stale >= cfg.deadman.stale_hours:
        health.reasons.append("no usable price in %.1f hours" % stale)
    if state.last_data is None and state.consecutive_failures >= cfg.deadman.fail_runs:
        health.reasons.append("never successfully fetched a price")
    if errors:
        health.reasons.append("last error: %s" % errors[-1])

    triggered = any(
        r
        for r in health.reasons
        if not r.startswith("last error")  # an error alone isn't a trigger
    )
    if not triggered:
        return health

    health.down = True
    if not state.deadman.get("down"):
        state.deadman = {
            "down": True,
            "since": now.isoformat(),
            "last_notified": now.isoformat(),
        }
        health.should_notify = True
        return health

    since_notified = hours_since(state.deadman.get("last_notified"), now)
    if since_notified is None or since_notified >= cfg.deadman.renotify_hours:
        state.deadman["last_notified"] = now.isoformat()
        health.should_notify = True
    return health
