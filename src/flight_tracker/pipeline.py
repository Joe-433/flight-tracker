"""A run in three stages, so the middle one can fan out across machines.

    plan    calendar scans, then choose which date pairs get a full search.
            One machine: the scans are cheap and the choice must be made once.
    drill   full searches for one shard of that choice. N machines in
            parallel, each with its own IP.
    apply   fold every shard's results into state, send alerts, commit.
            One machine: state has one writer per run.

Each stage hands the next a plain JSON document, which is what lets GitHub
Actions pass them between jobs as artifacts. Run locally, `run` calls all three
in-process with a single shard.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from . import planner
from .config import Config
from .sources import ScrapeError, get_source
from .sources.base import Offer
from .sources.grid import GridUnavailable, combos, get_grid
from .state import State, hours_since, utcnow

PLAN_VERSION = 1


def make_plan(
    cfg: Config,
    state: State,
    shards: Optional[int] = None,
    budget: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Stage 1. Scan the calendar where it's due, then choose the drills."""
    now = now or utcnow()
    shards = max(1, shards or cfg.source.shards)
    doc: Dict[str, Any] = {
        "version": PLAN_VERSION,
        "created": now.isoformat(),
        "grid": {},
        "grid_status": "disabled",
        "grid_scans": 0,
        "grid_failed": 0,
        "grid_requests": 0,
        "grid_errors": [],
    }

    if cfg.grid.enabled:
        try:
            scanner = get_grid(cfg)
        except GridUnavailable as exc:
            scanner = None
            doc["grid_status"] = "unavailable"
            doc["grid_errors"].append(str(exc))

        if scanner is not None:
            due = [
                combo
                for combo in combos(cfg)
                if _grid_due(state, combo.key, cfg.grid.refresh_minutes, now)
            ]
            for combo in due:
                try:
                    prices = scanner.scan(combo, today=now.date())
                except GridUnavailable as exc:
                    doc["grid_errors"].append(str(exc))
                    doc["grid_failed"] += len(due) - doc["grid_scans"]
                    doc["grid_scans"] = len(due)
                    break
                doc["grid_scans"] += 1
                if prices is None:
                    doc["grid_failed"] += 1
                else:
                    doc["grid"][combo.key] = {"seen": now.isoformat(), "prices": prices}
            doc["grid_requests"] = scanner.requests
            doc["grid_errors"].extend(scanner.errors)
            if not due:
                doc["grid_status"] = "skipped"
            elif doc["grid"]:
                doc["grid_status"] = "ok"
            else:
                doc["grid_status"] = "failed"

    fresh = {key: entry["prices"] for key, entry in doc["grid"].items()}
    candidates = planner.plan(cfg, state, budget=budget, grid=fresh, now=now)
    doc["shards"] = [
        [list(item) for item in shard]
        for shard in planner.partition(candidates, shards)
    ]
    doc["reasons"] = [
        [c.key, c.reason, round(c.overdue, 2), c.price] for c in candidates
    ]
    return doc


def _grid_due(state: State, key: str, refresh_minutes: float, now: dt.datetime) -> bool:
    entry = state.grid.get(key)
    if not entry:
        return True
    age = hours_since(str(entry.get("seen") or ""), now)
    return age is None or age * 60.0 >= refresh_minutes


def run_drills(
    cfg: Config, plan_doc: Dict[str, Any], shard: int
) -> Dict[str, Any]:
    """Stage 2. Full searches for one shard of the plan."""
    shards = plan_doc.get("shards") or [[]]
    items = [tuple(item) for item in shards[shard]] if shard < len(shards) else []
    offers: List[Offer] = []
    errors: List[str] = []
    try:
        source = get_source(cfg)
        offers = source.drill(items)  # type: ignore[arg-type]
        errors = list(source.errors)
    except ScrapeError as exc:
        errors = [str(exc)]
    except Exception as exc:  # unexpected, but the apply stage must still run
        errors = ["unhandled: %r" % exc]
    return {
        "shard": shard,
        "finished": utcnow().isoformat(),
        "checked": [planner.item_key(item) for item in items],  # type: ignore[arg-type]
        "offers": [asdict(offer) for offer in offers],
        "errors": errors,
    }


def offers_from(results: List[Dict[str, Any]]) -> List[Offer]:
    out = []
    for result in results:
        for raw in result.get("offers") or []:
            try:
                out.append(Offer(**raw))
            except TypeError:
                continue  # a result written by a different version; skip it
    return out
