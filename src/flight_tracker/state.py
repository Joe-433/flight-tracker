"""Persistent state: price history, alert dedup, and health counters.

Stored as pretty-printed JSON with sorted keys so the file is git-diffable --
committing it back each run doubles as price history AND keeps the repo
"active", which stops GitHub from auto-disabling the cron after 60 days.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Config
from .sources.base import Offer

SCHEMA_VERSION = 1
RESAMPLE_HOURS = 6.0  # log an unchanged price at most this often


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def hours_since(value: Optional[str], now: Optional[dt.datetime] = None) -> Optional[float]:
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return ((now or utcnow()) - parsed).total_seconds() / 3600.0


@dataclass
class State:
    version: int = SCHEMA_VERSION
    last_run: Optional[str] = None
    last_data: Optional[str] = None
    consecutive_failures: int = 0
    cursor: int = 0
    deadman: Dict[str, Optional[str]] = field(
        default_factory=lambda: {"down": False, "since": None, "last_notified": None}
    )
    alerts: Dict[str, Dict[str, object]] = field(default_factory=dict)
    observations: Dict[str, List[List[object]]] = field(default_factory=dict)

    # -- io -----------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "State":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("version") != SCHEMA_VERSION:
            # Forward-compatible enough: start clean rather than misread history.
            return cls()
        state = cls()
        for key in (
            "last_run",
            "last_data",
            "consecutive_failures",
            "cursor",
            "deadman",
            "alerts",
            "observations",
        ):
            if key in raw:
                setattr(state, key, raw[key])
        return state

    def save(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "version": self.version,
            "last_run": self.last_run,
            "last_data": self.last_data,
            "consecutive_failures": self.consecutive_failures,
            "cursor": self.cursor,
            "deadman": self.deadman,
            "alerts": self.alerts,
            "observations": self.observations,
        }
        # Atomic write: a half-written state file would look like a fresh start
        # and silently wipe price history.
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- history ------------------------------------------------------------

    def record(self, offers: List[Offer], now: Optional[dt.datetime] = None) -> int:
        """Append observations, skipping redundant repeats. Returns count added."""
        now = now or utcnow()
        stamp = now.isoformat()
        added = 0
        for offer in offers:
            series = self.observations.setdefault(offer.key, [])
            if series:
                last_ts, last_price = series[-1][0], float(series[-1][1])
                elapsed = hours_since(str(last_ts), now) or 0.0
                if last_price == offer.price and elapsed < RESAMPLE_HOURS:
                    continue
            series.append([stamp, offer.price])
            added += 1
        return added

    def trim(self, history_days: int, now: Optional[dt.datetime] = None) -> None:
        """Drop stale observations, past date pairs, and expired alert records."""
        now = now or utcnow()
        cutoff = now - dt.timedelta(days=history_days)
        today = now.date().isoformat()

        for key in list(self.observations):
            out_date = key.split("|", 1)[0]
            if out_date < today:  # the trip already departed; history is dead weight
                del self.observations[key]
                continue
            kept = [
                point
                for point in self.observations[key]
                if (parse_ts(str(point[0])) or now) >= cutoff
            ]
            if kept:
                self.observations[key] = kept
            else:
                del self.observations[key]

        for key in list(self.alerts):
            if key.split("|", 1)[0] < today:
                del self.alerts[key]

    def series(self, key: str) -> List[Tuple[str, float]]:
        return [(str(ts), float(price)) for ts, price in self.observations.get(key, [])]

    def all_prices(self) -> List[float]:
        return [
            float(price)
            for points in self.observations.values()
            for _, price in points
        ]

    # -- alert dedup --------------------------------------------------------

    def should_alert(
        self, offer: Offer, cfg: Config, now: Optional[dt.datetime] = None
    ) -> bool:
        """True unless we already shouted about this fare recently.

        Inside the cooldown we stay quiet, UNLESS the price fell by at least
        `rebeat_drop_usd` again -- a fare that keeps dropping is worth a second
        ping.
        """
        now = now or utcnow()
        prior = self.alerts.get(offer.key)
        if not prior:
            return True
        elapsed = hours_since(str(prior.get("ts")), now)
        if elapsed is None or elapsed >= cfg.alerts.cooldown_hours:
            return True
        return offer.price <= float(prior.get("price", 0)) - cfg.alerts.rebeat_drop_usd

    def mark_alerted(self, offer: Offer, now: Optional[dt.datetime] = None) -> None:
        self.alerts[offer.key] = {
            "price": offer.price,
            "ts": (now or utcnow()).isoformat(),
        }
