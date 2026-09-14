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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Config
from .sources.base import Offer, parse_key

SCHEMA_VERSION = 3


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


def _migrate_v2(raw: Dict[str, Any]) -> Dict[str, Any]:
    """v2 keys were `out|ret` or `out|ret|stops`; v3 adds the arrival airport.

    Every v2 observation came from the LA city MID, which returns LAX and
    nothing else, so the airport is known. Worth migrating rather than
    wiping: the record-low bars are the only alerting that's actually firing
    on this route, and they live in this file.
    """

    def upgrade(key: str) -> str:
        parts = key.split("|")
        if len(parts) == 2:
            return "%s|%s|LAX|0" % (parts[0], parts[1])
        if len(parts) == 3:
            return "%s|%s|LAX|%s" % (parts[0], parts[1], parts[2])
        return key

    for field_name in ("observations", "latest", "alerts"):
        section = raw.get(field_name)
        if isinstance(section, dict):
            raw[field_name] = {upgrade(k): v for k, v in section.items()}
    raw["version"] = SCHEMA_VERSION
    return raw


@dataclass
class State:
    version: int = SCHEMA_VERSION
    last_run: Optional[str] = None
    last_data: Optional[str] = None
    consecutive_failures: int = 0
    cursors: Dict[str, int] = field(default_factory=dict)
    deadman: Dict[str, Optional[str]] = field(
        default_factory=lambda: {"down": False, "since": None, "last_notified": None}
    )
    alerts: Dict[str, Dict[str, object]] = field(default_factory=dict)
    observations: Dict[str, List[List[object]]] = field(default_factory=dict)
    # Latest full detail per date pair (times, airports, airline). The
    # observation series stays price-only so it stays small and diffable;
    # this holds the one snapshot the reports actually render.
    latest: Dict[str, Dict[str, object]] = field(default_factory=dict)
    # Cheapest fare ever seen, per stop class. The bar for record-low alerts.
    records: Dict[str, Dict[str, object]] = field(default_factory=dict)

    # -- io -----------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "State":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("version") == 2:
            raw = _migrate_v2(raw)
        if raw.get("version") != SCHEMA_VERSION:
            # Forward-compatible enough: start clean rather than misread history.
            return cls()
        state = cls()
        for key in (
            "last_run",
            "last_data",
            "consecutive_failures",
            "cursors",
            "deadman",
            "alerts",
            "observations",
            "latest",
            "records",
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
            "cursors": self.cursors,
            "deadman": self.deadman,
            "alerts": self.alerts,
            "observations": self.observations,
            "latest": self.latest,
            "records": self.records,
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

    # -- merging ------------------------------------------------------------

    def merge(self, other: "State") -> "State":
        """Fold another copy of this state into this one. Returns self.

        Two sweeps that overlap each write the whole file, so whoever pushes
        second would otherwise discard the other's work -- and git can't
        resolve it, because there is no meaningful line-level merge of a
        generated JSON document. Union the data instead, which is well defined
        here: observations are append-only, and every other field has an
        obvious winner.
        """
        for key, points in other.observations.items():
            if key not in self.observations:
                self.observations[key] = list(points)
                continue
            seen = {str(point[0]) for point in self.observations[key]}
            self.observations[key].extend(
                point for point in points if str(point[0]) not in seen
            )
            self.observations[key].sort(key=lambda point: str(point[0]))

        # Freshest snapshot wins; it's what the reports render.
        for key, snapshot in other.latest.items():
            mine = self.latest.get(key)
            if not mine or str(snapshot.get("seen") or "") > str(mine.get("seen") or ""):
                self.latest[key] = snapshot

        # Most recent alert wins, so a cooldown is never accidentally reset.
        for key, record in other.alerts.items():
            mine = self.alerts.get(key)
            if not mine or str(record.get("ts") or "") > str(mine.get("ts") or ""):
                self.alerts[key] = record

        # Lowest price wins: a record low is a fact about the route, not about
        # which run happened to see it.
        for name, record in other.records.items():
            mine = self.records.get(name)
            if not mine or float(record.get("price", 0)) < float(mine.get("price", 0)):
                self.records[name] = record

        # Furthest cursor wins, so the losing run's ground isn't re-swept.
        for name, position in other.cursors.items():
            self.cursors[name] = max(int(position), int(self.cursors.get(name, 0)))

        self.last_run = max(self.last_run or "", other.last_run or "") or None
        self.last_data = max(self.last_data or "", other.last_data or "") or None
        # A success anywhere clears the failure streak.
        self.consecutive_failures = min(
            int(self.consecutive_failures or 0), int(other.consecutive_failures or 0)
        )
        if not other.deadman.get("down"):
            self.deadman = other.deadman if not self.deadman.get("down") else self.deadman
        return self

    # -- history ------------------------------------------------------------

    def record(
        self,
        offers: List[Offer],
        cfg: Optional[Any] = None,
        now: Optional[dt.datetime] = None,
    ) -> int:
        """Append observations, skipping redundant repeats. Returns count added.

        With hundreds of tracked date pairs and a commit every run, unfiltered
        logging would bloat state.json into a multi-megabyte file that churns
        constantly. So a point is kept only if the price actually moved by more
        than `min_change_usd`, or enough time has passed to be worth a fresh
        data point.
        """
        now = now or utcnow()
        stamp = now.isoformat()
        resample_hours = getattr(cfg, "resample_hours", 12.0) if cfg else 12.0
        min_change = getattr(cfg, "min_change_usd", 5.0) if cfg else 5.0
        cap = int(getattr(cfg, "max_points_per_pair", 40)) if cfg else 40

        added = 0
        for offer in offers:
            snapshot = offer.snapshot()
            snapshot["seen"] = stamp
            self.latest[offer.key] = snapshot

            series = self.observations.setdefault(offer.key, [])
            if series:
                last_ts, last_price = series[-1][0], float(series[-1][1])
                elapsed = hours_since(str(last_ts), now) or 0.0
                moved = abs(last_price - offer.price) >= min_change
                if not moved and elapsed < resample_hours:
                    continue
            series.append([stamp, offer.price])
            if len(series) > cap:
                del series[: len(series) - cap]
            added += 1
        return added

    def trim(
        self,
        history_days: int,
        now: Optional[dt.datetime] = None,
        max_points_per_pair: Optional[int] = None,
        allowed_nights: Optional[Iterable[int]] = None,
        stale_hours: Optional[float] = None,
    ) -> None:
        """Drop stale observations, past date pairs, and expired alert records.

        `allowed_nights` prunes trip lengths that are no longer searched. Without
        it, narrowing `trip_nights` would leave zombie entries: never refreshed
        again, never expiring until their departure date passes, and sitting at
        the top of the cheapest-fares report the whole time.
        """
        now = now or utcnow()
        cutoff = now - dt.timedelta(days=history_days)
        today = now.date().isoformat()
        nights = set(allowed_nights) if allowed_nights is not None else None

        def out_of_scope(key: str) -> bool:
            if nights is None:
                return False
            out_date, ret_date, _airport, _stops = parse_key(key)
            try:
                span = (
                    dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)
                ).days
            except ValueError:
                return False
            return span not in nights

        for key in list(self.observations):
            out_date = key.split("|", 1)[0]
            if out_date < today:  # the trip already departed; history is dead weight
                del self.observations[key]
                continue
            if out_of_scope(key):  # trip length is no longer searched
                del self.observations[key]
                continue
            kept = [
                point
                for point in self.observations[key]
                if (parse_ts(str(point[0])) or now) >= cutoff
            ]
            if kept:
                if max_points_per_pair and len(kept) > max_points_per_pair:
                    kept = kept[-max_points_per_pair:]
                self.observations[key] = kept
            else:
                del self.observations[key]

        for key in list(self.alerts):
            if key.split("|", 1)[0] < today:
                del self.alerts[key]

        for key in list(self.latest):
            if key.split("|", 1)[0] < today or key not in self.observations:
                del self.latest[key]
                continue
            # A fare we've stopped re-checking -- because the search window
            # moved past it, or its band starved -- must not keep quoting a
            # price that may no longer exist.
            if stale_hours is not None:
                age = hours_since(str(self.latest[key].get("seen") or ""), now)
                if age is not None and age > stale_hours:
                    del self.latest[key]

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
