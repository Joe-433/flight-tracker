"""Command line entry point.

    python -m flight_tracker run           # one sweep, alert if warranted
    python -m flight_tracker days          # low-price-day table from history
    python -m flight_tracker test-alert    # prove the notification path works
    python -m flight_tracker verify        # one live query, sanity-check it
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import time
import sys
from typing import Callable, Dict, List, Optional, Tuple

from . import analysis, deadman
from .booking import airline_url, carrier_of
from .config import Config, load_config
from .notify import BLURPLE, GREEN, Message, Notifier
from .sources import ScrapeError, get_source
from .sources.base import Offer, date_pairs
from .state import State, hours_since as _hours_since, utcnow

DEFAULT_CONFIG = "config.yaml"
DEFAULT_STATE = os.path.join("data", "state.json")


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def _money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return "%s%s" % (symbol, ("%.0f" % value))


def _pretty_date(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%a %b %-d")


REASON_TEXT = {
    "threshold": "under your %s threshold",
    "baseline": "%s below its own recent median",
    "percentile": "in the cheapest %s of fares we've logged",
}


def describe(deal: analysis.Deal, cfg: Config) -> str:
    bits: List[str] = []
    for reason in deal.reasons:
        if reason == "threshold":
            bits.append(
                REASON_TEXT[reason]
                % _money(
                    analysis.threshold_for(deal.offer, cfg), cfg.search.currency
                )
            )
        elif reason == "baseline" and deal.discount is not None:
            bits.append(REASON_TEXT[reason] % ("%.0f%%" % (deal.discount * 100)))
        elif reason == "percentile":
            bits.append(
                REASON_TEXT[reason] % ("%.0f%%" % (cfg.deals.cheap_percentile * 100))
            )
    return "; ".join(bits)


def deal_message(deal: analysis.Deal, cfg: Config) -> Message:
    offer = deal.offer
    airlines = ", ".join(offer.airlines) if offer.airlines else "see link"
    fields = [
        (
            "When",
            "%s \u2192 %s  \u00b7  %d nights"
            % (
                _pretty_date(offer.out_date),
                _pretty_date(offer.ret_date),
                offer.nights,
            ),
        ),
        (
            "Flight",
            "%s  \u00b7  %s\u2192%s  \u00b7  %s"
            % (
                _clock12(offer.dep_time).strip(),
                offer.dep_airport or "???",
                offer.arr_airport or "???",
                offer.flight_no or airlines,
            ),
        ),
        ("Why", describe(deal, cfg) or "cheapest in this sweep"),
    ]
    if deal.baseline:
        fields.append(
            ("Recent median for these dates", _money(deal.baseline, offer.currency))
        )
    return Message(
        title="%s  ·  %s → %s  ·  %s"
        % (
            _money(offer.price, offer.currency),
            cfg.route.origin_label,
            cfg.route.destination_label,
            offer.label,
        ),
        fields=fields,
        url=offer.url,  # makes the embed title itself tappable
        urgent=True,
        color=GREEN,
        footer="Tap the title to book",
    )


def record_message(record: analysis.RecordLow, cfg: Config) -> Message:
    offer = record.offer
    airlines = ", ".join(offer.airlines) if offer.airlines else "see link"
    age = ""
    hours = _hours_since(record.previous_seen)
    if hours is not None:
        age = " \u00b7 set %s ago" % (
            "%dh" % hours if hours < 48 else "%dd" % (hours / 24)
        )
    return Message(
        title="New low  \u00b7  %s  \u00b7  %s \u2192 %s  \u00b7  %s"
        % (
            _money(offer.price, offer.currency),
            cfg.route.origin_label,
            cfg.route.destination_label,
            offer.label,
        ),
        fields=[
            (
                "Beats the previous %s low" % analysis.stop_class(offer),
                "%s, down %s%s"
                % (_money(record.previous), _money(record.saving), age),
            ),
            (
                "When",
                "%s \u2192 %s  \u00b7  %d nights"
                % (
                    _pretty_date(offer.out_date),
                    _pretty_date(offer.ret_date),
                    offer.nights,
                ),
            ),
            (
                "Flight",
                "%s  \u00b7  %s\u2192%s  \u00b7  %s"
                % (
                    _clock12(offer.dep_time).strip(),
                    offer.dep_airport or "???",
                    offer.arr_airport or "???",
                    offer.flight_no or airlines,
                ),
            ),
        ],
        url=offer.url,
        color=BLURPLE,  # informational: no @here, this is not the $250 alarm
        footer="Still above your alert threshold \u00b7 tap the title to book",
    )


def days_table(stats: List[analysis.DayStat], cfg: Config, limit: int = 14) -> str:
    if not stats:
        return "No price history yet."
    rows = [
        "```",
        "%-14s %-14s %8s %-8s %s"
        % ("DEPART", "RETURN", "PRICE", "STOPS", ""),
    ]
    for stat in stats[:limit]:
        rows.append(
            "%-14s %-14s %8s %-8s %s"
            % (
                _pretty_date(stat.out_date),
                _pretty_date(stat.ret_date),
                _money(stat.price, cfg.search.currency),
                "nonstop" if not stat.stops else "%d stop" % stat.stops,
                "<- cheap day" if stat.cheap else "",
            )
        )
    rows.append("```")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.backend:
        cfg.source.backend = args.backend
    if args.threshold is not None:
        cfg.alerts.threshold_usd = args.threshold
    if args.pairs is not None:
        cfg.source.pairs_per_run = args.pairs

    state = State.load(args.state)
    notifier = Notifier.from_env(force_console=args.console)
    now = utcnow()

    offers: List[Offer] = []
    errors: List[str] = []
    next_cursors = dict(state.cursors)
    try:
        source = get_source(cfg)
        offers, next_cursors = source.sweep(state.cursors)
        errors = source.errors
    except ScrapeError as exc:
        errors = [str(exc)]
        print("scrape failed: %s" % exc)
    except Exception as exc:  # unexpected, but must not skip the health update
        errors = ["unhandled: %r" % exc]
        print("unexpected failure: %r" % exc)

    print(
        "%s sweep: %d offers, %d errors (backend=%s)"
        % (now.isoformat(timespec="seconds"), len(offers), len(errors), cfg.source.backend)
    )
    # Partial failures are normal (a date pair with no nonstops at all will
    # error), but a silent count tells you nothing about which kind you have.
    for error in errors[:5]:
        print("  ! %s" % error[:200])
    if len(errors) > 5:
        print("  ! ...and %d more" % (len(errors) - 5))

    # Assess BEFORE recording, so a fare is never part of its own baseline.
    assessment = analysis.assess(offers, state, cfg, now=now)

    state.record(offers, cfg.history, now=now)
    state.cursors = next_cursors
    state.trim(
        cfg.history.days,
        now=now,
        max_points_per_pair=cfg.history.max_points_per_pair,
        allowed_nights=cfg.search.trip_nights,
        stale_hours=cfg.history.stale_hours,
        exclude_origins=cfg.route.exclude_origins,
    )

    health = deadman.update(state, cfg, got_data=bool(offers), errors=errors, now=now)

    if health.should_notify and not args.no_notify:
        notifier.send(_health_message(health, cfg, state))
    if health.down:
        print("DEAD MAN'S SWITCH: %s" % "; ".join(health.reasons))

    sent = 0
    for deal in assessment.alerts:
        if sent >= cfg.alerts.max_per_run:
            break
        if not state.should_alert(deal.offer, cfg, now=now):
            continue
        print(
            "alert: %s %s->%s (%s)"
            % (
                _money(deal.offer.price, deal.offer.currency),
                deal.offer.out_date,
                deal.offer.ret_date,
                ",".join(deal.reasons),
            )
        )
        if not args.no_notify:
            notifier.send(deal_message(deal, cfg))
        state.mark_alerted(deal.offer, now=now)
        sent += 1

    for record in analysis.claim_record_lows(offers, state, cfg, now=now):
        print(
            "record low: %s (%s), was %s"
            % (
                _money(record.offer.price, record.offer.currency),
                analysis.stop_class(record.offer),
                _money(record.previous),
            )
        )
        if not args.no_notify:
            notifier.send(record_message(record, cfg))

    if assessment.cheapest:
        print(
            "cheapest this sweep: %s on %s -> %s"
            % (
                _money(assessment.cheapest.price, assessment.cheapest.currency),
                assessment.cheapest.out_date,
                assessment.cheapest.ret_date,
            )
        )
    if assessment.cheap_cutoff:
        print("cheap-day cutoff: %s" % _money(assessment.cheap_cutoff))
    print(
        "alerts sent: %d | tracked pairs: %d | cursors: %s"
        % (sent, len(state.observations), state.cursors)
    )

    state.save(args.state)
    if health.down and args.fail_on_down:
        return 1
    return 0


def _health_message(health: deadman.Health, cfg: Config, state: State) -> Message:
    if health.recovered:
        return Message(
            title="✅ Flight tracker is back",
            body="Fares are being fetched again. Normal alerting resumed.",
        )
    return Message(
        title="⚠️ Flight tracker is not getting data",
        body="\n".join(
            [
                "The sweep stopped returning fares. This is almost always the",
                "scraper breaking, not the route being empty.",
                "",
                "Reasons: %s" % "; ".join(health.reasons),
                "Last good data: %s" % (state.last_data or "never"),
                "",
                "Check: pip install -U fast-flights, then "
                "`python -m flight_tracker verify`.",
            ]
        ),
        urgent=True,
    )


def cmd_days(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    history = state.all_prices()
    needed = analysis.min_route_samples(cfg.deals.cheap_percentile)
    cutoff = (
        analysis.percentile(history, cfg.deals.cheap_percentile)
        if len(history) >= needed
        else None
    )
    stats = analysis.day_stats([], state, cutoff)
    table = days_table(stats, cfg, limit=args.limit)
    body = table
    if cutoff:
        body += "\nCheap-day cutoff: %s (bottom %.0f%% of %d observations)" % (
            _money(cutoff),
            cfg.deals.cheap_percentile * 100,
            len(history),
        )
    else:
        body += "\nNot enough history yet for a cheap-day cutoff (%d/%d observations)." % (
            len(history),
            needed,
        )

    message = Message(
        title="Cheapest %s \u2192 %s days"
        % (cfg.route.origin_label, cfg.route.destination_label),
        body=body,
    )
    if args.send:
        Notifier.from_env().send(message)
    else:
        print(message.as_text())
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    notifier = Notifier.from_env(force_console=args.console)
    print("channels: %s" % ", ".join(notifier.channel_names))
    results = notifier.send(
        Message(
            title="✈️ Flight tracker test alert",
            body="If you're reading this, alerting works.\nRoute: %s -> %s, nonstop, "
            "threshold %s."
            % (
                cfg.route.origin_label,
                cfg.route.destination_label,
                _money(cfg.alerts.threshold_usd, cfg.search.currency),
            ),
            url="https://www.google.com/travel/flights",
        )
    )
    ok = all(success for _, success, _ in results)
    for name, success, error in results:
        print("  %-8s %s%s" % (name, "ok" if success else "FAILED", "" if success else ": " + str(error)))
    return 0 if ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """One live query, printed in full. Run this before trusting any alert."""
    cfg = load_config(args.config)
    cfg.source.backend = args.backend or cfg.source.backend
    pairs = date_pairs(cfg)
    if not pairs:
        print("no date pairs in window; check search config")
        return 1
    out_date, ret_date = pairs[len(pairs) // 2]

    print("config : %s" % args.config)
    print("route  : %s -> %s" % (cfg.route.origin, cfg.route.destination))
    print("dates  : %s -> %s" % (out_date, ret_date))
    print("filters: max_stops=%d carry_on=%d checked=%d %s"
          % (cfg.search.max_stops, cfg.search.carry_on_bags,
             cfg.search.checked_bags, cfg.search.currency))

    try:
        source = get_source(cfg)
    except ScrapeError as exc:
        print("FAIL: %s" % exc)
        return 1

    fetch = getattr(source, "fetch_pair", None)
    if fetch is None:
        offers, _ = source.sweep(0)
        offer = offers[0] if offers else None
    else:
        query = source._query(out_date, ret_date)  # noqa: SLF001 - diagnostics
        print("url    : %s" % query.url())
        offer = fetch(out_date, ret_date)

    if source.errors:
        print("errors : %s" % "; ".join(source.errors))
    if offer is None:
        print("FAIL: no fare returned. Either the MIDs are wrong, there are no")
        print("      nonstops on these dates, or the scraper is broken.")
        return 1

    print("price  : %s" % _money(offer.price, offer.currency))
    print("airline: %s" % (", ".join(offer.airlines) or "unknown"))
    print("stops  : %s" % ("unknown" if offer.stops is None else offer.stops))
    print("")
    print("NOW CHECK BY HAND: open the url above, switch the stops filter to")
    print("'Nonstop only', and confirm the price matches. If it doesn't, the")
    print("nonstop filter is being ignored and every alert will be wrong.")
    return 0


# ---------------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    """Sample fares across an arbitrary lead-time range. Read-only.

    This is the tool for answering "is it worth searching that far out?" --
    it touches no state, sends no alerts, and can look anywhere on the
    calendar regardless of what config.yaml says the live window is.
    """
    cfg = load_config(args.config)
    cfg.search.min_days_ahead = args.min_days
    cfg.search.window_days = args.window
    if args.nights:
        cfg.search.trip_nights = args.nights
    if args.carry_on is not None:
        cfg.search.carry_on_bags = args.carry_on
    if args.max_stops is not None:
        cfg.search.max_stops = args.max_stops
    if args.to:
        cfg.route.destination = args.to
    cfg.source.bands = []
    cfg.route.also_check = []

    today = dt.date.today()
    pairs = date_pairs(cfg, today)
    if not pairs:
        print("no date pairs in that range")
        return 1

    # Spread the samples evenly across the range instead of taking a
    # contiguous block, so the answer isn't one week's worth of weather.
    stride = max(1, len(pairs) // args.pairs)
    sampled = pairs[::stride][: args.pairs]

    print(
        "probing %d-%d days out: %d of %d pairs | nights=%s carry_on=%d "
        "max_stops=%d"
        % (args.min_days, args.min_days + args.window, len(sampled), len(pairs),
           cfg.search.trip_nights, cfg.search.carry_on_bags, cfg.search.max_stops)
    )

    try:
        source = get_source(cfg)
    except ScrapeError as exc:
        print("FAIL: %s" % exc)
        return 1

    results: List[Offer] = []
    import random as _random
    import time as _time

    for i, (out_date, ret_date) in enumerate(sampled):
        if i:
            _time.sleep(_random.uniform(2, 5))
        offer = source.fetch_pair(out_date, ret_date)
        lead = (dt.date.fromisoformat(out_date) - today).days
        if offer is None:
            print("  %3d days  %s -> %s   no fare" % (lead, out_date, ret_date))
            continue
        results.append(offer)
        print(
            "  %3d days  %s -> %s   %s  %s"
            % (lead, out_date, ret_date, _money(offer.price, offer.currency),
               ", ".join(offer.airlines) or "?")
        )

    if not results:
        print("\nno fares at all in this range")
        return 1

    prices = sorted(o.price for o in results)
    print(
        "\n%d fares | min %s | median %s | max %s"
        % (
            len(prices),
            _money(prices[0]),
            _money(analysis.median(prices) or 0),
            _money(prices[-1]),
        )
    )
    if source.errors:
        print("errors: %d" % len(source.errors))
    return 0


def _clock12(value: Optional[str]) -> str:
    """"07:05" -> "7:05a". Times are the third thing you read; keep them narrow."""
    if not value or ":" not in value:
        return "  --  "
    try:
        hour, minute = (int(x) for x in value.split(":", 1))
    except ValueError:
        return "  --  "
    suffix = "a" if hour < 12 else "p"
    display = hour % 12 or 12
    return "%d:%02d%s" % (display, minute, suffix)


def _short_date(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%a %b %-d")


def _row_parts(price: float, snap: dict, cfg: Config) -> List[str]:
    out_date = str(snap.get("out_date", ""))
    ret_date = str(snap.get("ret_date", ""))
    nights = ""
    if out_date and ret_date:
        nights = str(
            (
                dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)
            ).days
        )

    stops = snap.get("stops")
    stops_text = (
        "nonstop"
        if stops == 0
        else ("%s stop" % stops if isinstance(stops, int) else "?")
    )
    airlines = snap.get("airlines") or []
    # The flight number is shorter than the airline name and more useful: it
    # already names the carrier, and it's what a booking site wants.
    who = str(snap.get("flight_no") or "") or (
        ", ".join(str(a) for a in airlines)[:12] or "?"
    )

    return [
        _money(price, cfg.search.currency),
        _short_date(out_date) if out_date else "?",
        _short_date(ret_date) if ret_date else "?",
        nights,
        _clock12(snap.get("dep_time")).strip(),
        _clock12(snap.get("arr_time")).strip(),
        "%s\u2192%s" % (snap.get("dep_airport") or "???", snap.get("arr_airport") or "???"),
        stops_text,
        who,
    ]


def _dedupe_key(snap: dict) -> tuple:
    """What makes two rows the same fare rather than two options.

    The sweep checks every return date against every departure date, so one
    cheap outbound flight shows up once per return date -- three rows, same
    price, same plane, same seat. That's one option presented as three, and it
    crowds genuinely different fares out of a top-5.
    """
    return (
        round(float(snap.get("price") or 0)),
        str(snap.get("flight_no") or snap.get("dep_time") or ""),
        str(snap.get("dep_airport") or ""),
        str(snap.get("arr_airport") or ""),
        snap.get("stops"),
    )


def _nights(snap: dict) -> int:
    try:
        return (
            dt.date.fromisoformat(str(snap["ret_date"]))
            - dt.date.fromisoformat(str(snap["out_date"]))
        ).days
    except (KeyError, TypeError, ValueError):
        return 99


def _fare_line(
    price: float,
    snap: dict,
    extras: int,
    cfg: Config,
    linked: bool = True,
    now: Optional[dt.datetime] = None,
) -> str:
    """One fare as ordinary prose, not a table cell.

    A fixed-width table is only readable while it fits the reader's window; at
    ~82 characters Discord breaks every row mid-cell and the alignment that
    justified the monospace font is exactly what's destroyed. Normal text
    reflows at a separator instead, so a narrow window costs a wrapped line
    rather than a mangled grid.
    """
    stops = snap.get("stops")
    stops_text = (
        "nonstop"
        if stops == 0
        else ("%s stop" % stops if isinstance(stops, int) else "? stops")
    )
    airlines = snap.get("airlines") or []
    flight_no = str(snap.get("flight_no") or "")
    who = flight_no or (", ".join(str(a) for a in airlines)[:20] or "?")
    out_date = str(snap.get("out_date", ""))
    ret_date = str(snap.get("ret_date", ""))

    # Link the flight code to the airline's own booking search where the deep
    # link is known to work. Carriers whose format couldn't be verified are
    # left unlinked rather than sent somewhere broken.
    direct = airline_url(
        carrier_of(flight_no),
        str(snap.get("dep_airport") or ""),
        str(snap.get("arr_airport") or ""),
        out_date,
        ret_date,
        cfg.search.adults,
    )
    if linked and direct:
        who = "[%s](%s)" % (who, direct)

    # The price is the link. A flight number is a poor handle on a fare months
    # out -- schedules move and the number alone won't reconstruct the search --
    # whereas the deeplink reopens the exact query that found this price.
    # Masked links render in embeds; `linked=False` is the fallback for
    # channels and sizes where they don't.
    money = _money(price, cfg.search.currency)
    url = str(snap.get("url") or "")
    bits = [
        "**[%s](%s)**" % (money, url) if (linked and url) else "**%s**" % money,
        "%s \u2192 %s"
        % (
            _short_date(out_date) if out_date else "?",
            _short_date(ret_date) if ret_date else "?",
        ),
        "%dn" % _nights(snap),
        "%s\u2192%s" % (snap.get("dep_airport") or "???", snap.get("arr_airport") or "???"),
        stops_text,
        who,
    ]
    if extras:
        bits.append("+%d more %s" % (extras, "date" if extras == 1 else "dates"))

    # A price nobody can reproduce is worse than no price. Say how old it is
    # once it's old enough that the fare may well have moved.
    age = _hours_since(str(snap.get("seen") or ""), now)
    if age is not None and age >= 12:
        bits.append(
            "_seen %s ago_"
            % ("%dh" % round(age) if age < 48 else "%dd" % round(age / 24))
        )
    return "  \u00b7  ".join(bits)


def _grouped(
    state: State, cfg: Config, keep: Callable[[dict], bool]
) -> List[List[Tuple[float, dict]]]:
    """Fares matching `keep`, de-duplicated, cheapest group first."""
    ranked = []
    for snap in state.latest.values():
        if not keep(snap):
            continue
        try:
            ranked.append((float(snap["price"]), snap))  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
    ranked.sort(key=lambda r: (r[0], _nights(r[1])))

    groups: Dict[tuple, List[Tuple[float, dict]]] = {}
    order: List[tuple] = []
    for price, snap in ranked:
        key = _dedupe_key(snap)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((price, snap))
    return [groups[key] for key in order]


def _render(
    groups: List[List[Tuple[float, dict]]],
    limit: int,
    cfg: Config,
    linked: bool,
    now: Optional[dt.datetime] = None,
) -> List[str]:
    """Cheapest `limit` fares, listed in departure order.

    Ranking by price is what makes the list worth reading; reading it in date
    order is what makes it usable for planning a trip.
    """
    chosen = sorted(
        groups[:limit], key=lambda g: str(g[0][1].get("out_date") or "")
    )
    return [
        _fare_line(group[0][0], group[0][1], len(group) - 1, cfg, linked, now)
        for group in chosen
    ]


def _stops_of(snap: dict) -> int:
    value = snap.get("stops")
    return int(value) if isinstance(value, int) else 1


def cheapest_lines(
    state: State,
    cfg: Config,
    limit: int = 3,
    now: Optional[dt.datetime] = None,
) -> str:
    """Cheapest nonstops and cheapest one-stops, as separate lists.

    One ranked list doesn't work on this route: connecting fares are reliably
    cheaper, so they take every slot and the nonstops -- the reason the tracker
    exists -- never appear. Splitting the two means the cheap connection is
    still visible without it hiding what a direct flight costs.
    """
    nonstop = _grouped(state, cfg, lambda snap: _stops_of(snap) == 0)
    connecting = _grouped(state, cfg, lambda snap: _stops_of(snap) >= 1)
    if not nonstop and not connecting:
        return "No fares tracked yet."

    def compose(linked: bool) -> str:
        blocks = []
        for title, groups in (("Nonstop", nonstop), ("One layover", connecting)):
            lines = _render(groups, limit, cfg, linked, now)
            blocks.append(
                "**%s**\n%s"
                % (title, "\n\n".join(lines) if lines else "_none tracked yet_")
            )
        return "\n\n".join(blocks)

    body = compose(linked=True)
    # An embed description is capped at 4096 characters and these deeplinks run
    # ~200 each. Rather than truncate mid-link and break every row after it,
    # drop the links and keep the fares readable.
    if len(body) > 3900:
        body = compose(linked=False)
    return body


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    message = Message(
        title="Cheapest %s \u2192 %s \u00b7 top %d of each"
        % (cfg.route.origin_label, cfg.route.destination_label, args.limit),
        body=cheapest_lines(state, cfg, limit=args.limit),
        footer="Outbound airports \u00b7 prices as last seen \u00b7 "
        "%d date pairs tracked" % len(state.latest),
        color=GREEN,
    )
    if args.send:
        results = Notifier.from_env().send(message)
        for name, ok, error in results:
            print("  %-8s %s%s" % (name, "ok" if ok else "FAILED", "" if ok else ": %s" % error))
        return 0 if all(ok for _, ok, _ in results) else 1
    print(message.as_text())
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Cross-check extracted flight numbers against the parsed itineraries.

    The numbers come from a second read of the same payload, matched to
    itineraries by list position. Position matching is the weak point: if the
    two walks ever disagree about ordering, every flight number would be
    attached to the wrong flight and nothing would look obviously broken.

    The carrier code inside the flight-number field is the independent check.
    `['WN', '2536', None, 'Southwest']` must agree with the airline the parser
    reports for that same itinerary; if it doesn't, the lists are misaligned.
    """
    cfg = load_config(args.config)
    if args.to:
        cfg.route.destination = args.to
    if getattr(args, "from_", None):
        cfg.route.origin = args.from_
    pairs = date_pairs(cfg)

    from fast_flights import fetch_flights_html
    from fast_flights.parser import parse

    from .sources.pairs import _flight_numbers, _format_flight_no

    source = get_source(cfg)

    # Some date pairs come back in a shape the parser can't read. That's normal
    # and the sweep just skips them; a diagnostic should walk on to the next.
    results = numbers = None
    for offset in range(args.attempts):
        out_date, ret_date = pairs[(len(pairs) // 2 + offset * 7) % len(pairs)]
        html = fetch_flights_html(source._query(out_date, ret_date))  # noqa: SLF001
        try:
            results = parse(html)
        except Exception as exc:
            print("skipping %s -> %s: %s" % (out_date, ret_date, _explain_dump(exc)))
            time.sleep(3)
            continue
        numbers = _flight_numbers(html)
        break

    if not results:
        print("no date pair returned a parsable payload in %d attempts" % args.attempts)
        return 1

    print("dates   : %s -> %s" % (out_date, ret_date))
    print("parsed  : %d itineraries" % len(results))
    print("numbers : %d itineraries" % len(numbers))
    if len(results) != len(numbers):
        print("MISALIGNED: counts differ, flight numbers would be suppressed")
        return 1

    agree = disagree = unknown = 0
    from collections import Counter

    origins: "Counter[str]" = Counter()
    arrivals: "Counter[str]" = Counter()
    print("")
    print("%-8s %-10s %-20s %-18s %-10s %s" % ("PRICE", "ROUTE", "AIRLINE (parser)", "FLIGHT (payload)", "SEGMENTS", "MATCH"))
    for item, segment_numbers in zip(results, numbers):
        airlines = list(getattr(item, "airlines", []) or [])
        legs = list(getattr(item, "flights", []) or [])
        flight_no = _format_flight_no(segment_numbers, len(legs))
        carrier = (segment_numbers[0] or "").split(" ")[0] if segment_numbers else ""

        if not flight_no or not airlines:
            verdict, unknown = "?", unknown + 1
        else:
            # The payload gives the airline name alongside the code; the
            # parser reports names independently. They must describe the
            # same carrier.
            names = " ".join(airlines).lower()
            initials = "".join(word[0] for word in names.split() if word)
            verdict = "ok" if (
                carrier.lower() in names
                or carrier.lower() in initials
                or names.startswith(carrier[:2].lower())
                or _CARRIERS.get(carrier, "").lower() in names
            ) else "MISMATCH"
            if verdict == "ok":
                agree += 1
            else:
                disagree += 1

        route = "?"
        if legs:
            route = "%s\u2192%s" % (
                getattr(getattr(legs[0], "from_airport", None), "code", "?"),
                getattr(getattr(legs[-1], "to_airport", None), "code", "?"),
            )
            arrivals[getattr(getattr(legs[-1], "to_airport", None), "code", "?")] += 1
            origins[getattr(getattr(legs[0], "from_airport", None), "code", "?")] += 1
        print(
            "%-8s %-10s %-20s %-18s %-10s %s"
            % (
                getattr(item, "price", "?"),
                route,
                ", ".join(airlines)[:20],
                flight_no or "-",
                "%d leg(s)" % len(legs),
                verdict,
            )
        )

    print("")
    print("agree %d | MISMATCH %d | unknown %d" % (agree, disagree, unknown))
    print("")
    print("ORIGIN airports offered      : %s"
          % ", ".join("%s x%d" % kv for kv in origins.most_common()))
    print("DESTINATION airports offered : %s"
          % ", ".join("%s x%d" % kv for kv in arrivals.most_common()))
    print("")
    print("These are ALL itineraries Google returned, not just the cheapest --")
    print("so this shows whether the city MIDs really search the whole metro.")
    return 1 if disagree else 0


def _explain_dump(exc: Exception) -> str:
    if isinstance(exc, (IndexError, KeyError, TypeError)):
        return "unparsable payload (%r)" % exc
    return "%s: %s" % (type(exc).__name__, exc)


# Codes whose airline name shares no letters with the code.
_CARRIERS = {
    "WN": "Southwest",
    "B6": "JetBlue",
    "AS": "Alaska",
    "AA": "American",
    "DL": "Delta",
    "UA": "United",
    "NK": "Spirit",
    "F9": "Frontier",
    "HA": "Hawaiian",
    "SY": "Sun Country",
    "G4": "Allegiant",
}


def cmd_merge(args: argparse.Namespace) -> int:
    """Merge another state file into this one, in place.

    Used by the workflow when a concurrent push beat this run to the remote.
    """
    if not os.path.exists(args.other):
        print("nothing to merge: %s does not exist" % args.other)
        return 0
    cfg = load_config(args.config)
    mine = State.load(args.state)
    theirs = State.load(args.other)
    before = sum(len(v) for v in mine.observations.values())
    mine.merge(theirs)

    # Re-trim after merging. The remote copy still holds everything this run
    # just pruned, and a union can't tell "data you don't have" apart from
    # "data you deliberately deleted" -- so without this, every stale fare is
    # resurrected on every run and the reports quote week-old prices forever.
    stale_before = len(mine.latest)
    mine.trim(
        cfg.history.days,
        max_points_per_pair=cfg.history.max_points_per_pair,
        allowed_nights=cfg.search.trip_nights,
        stale_hours=cfg.history.stale_hours,
        exclude_origins=cfg.route.exclude_origins,
    )
    after = sum(len(v) for v in mine.observations.values())
    mine.save(args.state)
    print(
        "merged %s: %d -> %d observations | fresh fares %d -> %d"
        % (args.other, before, after, stale_before, len(mine.latest))
    )
    return 0


def cmd_grid(args: argparse.Namespace) -> int:
    """Run calendar scans and check them against full-search prices.

    Diagnostic. Answers two questions before the grid is trusted to steer
    anything: does the calendar endpoint answer at all, and when it names a
    price for a date pair, does a full search for that pair agree?
    """
    from .sources.grid import Combo, GridUnavailable, combos, get_grid

    cfg = load_config(args.config)
    state = State.load(args.state)
    try:
        scanner = get_grid(cfg)
    except GridUnavailable as exc:
        print("FAIL: %s" % exc)
        return 1
    if scanner is None:
        print("grid is disabled in config")
        return 1

    wanted = combos(cfg)
    if args.dest:
        wanted = [c for c in wanted if c.destination == args.dest.upper()]
    if args.nights:
        wanted = [c for c in wanted if c.nights == args.nights]
    if args.limit:
        wanted = wanted[: args.limit]

    print("origins %s | %d scans" % ("+".join(scanner.origins()), len(wanted)))
    print("")
    print("%-12s %6s %9s  %-12s" % ("SCAN", "DATES", "CHEAPEST", "ON"))

    matched = []
    failed = 0
    for combo in wanted:
        prices = scanner.scan(combo)
        if prices is None:
            failed += 1
            print("%-12s %6s" % (combo.key, "FAILED"))
            continue
        if not prices:
            print("%-12s %6d" % (combo.key, 0))
            continue
        day = min(prices, key=prices.get)
        print("%-12s %6d %9s  %-12s" % (combo.key, len(prices), _money(prices[day]), day))

        # Pair every calendar price with a full search for the same trip.
        for out_date, price in prices.items():
            ret_date = (
                dt.date.fromisoformat(out_date) + dt.timedelta(days=combo.nights)
            ).isoformat()
            key = "%s|%s|%s|%d" % (
                out_date, ret_date, combo.destination, 0 if combo.nonstop else 1
            )
            snap = state.latest.get(key)
            if snap:
                matched.append((combo, out_date, price, float(snap["price"]), snap))

    print("")
    print("requests: %d | failed scans: %d/%d" % (scanner.requests, failed, len(wanted)))
    for error in scanner.errors[:5]:
        print("  ! %s" % error[:200])

    if matched:
        deltas = [grid - drill for _, _, grid, drill, _ in matched]
        exact = sum(1 for d in deltas if abs(d) < 1)
        close = sum(1 for d in deltas if abs(d) <= 15)
        print("")
        print(
            "calendar vs full search on %d shared date pairs: %d exact, %d within $15"
            % (len(matched), exact, close)
        )
        print("median gap %s | worst %s" % (
            _money(analysis.median(deltas) or 0),
            _money(max(deltas, key=abs)),
        ))
        worst = sorted(matched, key=lambda m: -abs(m[2] - m[3]))[:5]
        for combo, out_date, grid, drill, snap in worst:
            print(
                "  %-12s %s  calendar %-6s full search %-6s (seen %s)"
                % (combo.key, out_date, _money(grid), _money(drill),
                   str(snap.get("seen", ""))[:16])
            )
    return 1 if failed == len(wanted) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flight_tracker", description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one sweep + alerting")
    run.add_argument("--backend", choices=["pairs", "grid", "mock"])
    run.add_argument("--threshold", type=float)
    run.add_argument(
        "--pairs",
        type=int,
        help="override source.pairs_per_run for this run (manual deep scan)",
    )
    run.add_argument("--no-notify", action="store_true", help="decide but don't send")
    run.add_argument("--console", action="store_true", help="also print alerts")
    run.add_argument("--fail-on-down", action="store_true", help="exit 1 if unhealthy")
    run.set_defaults(func=cmd_run)

    days = sub.add_parser("days", help="cheapest departure days from history")
    days.add_argument("--limit", type=int, default=14)
    days.add_argument("--send", action="store_true", help="push the table to alerts")
    days.set_defaults(func=cmd_days)

    report = sub.add_parser("report", help="top N cheapest fares, formatted")
    report.add_argument("--limit", type=int, default=3, help="per section")
    report.add_argument("--send", action="store_true")
    report.set_defaults(func=cmd_report)

    merge = sub.add_parser(
        "merge", help="merge another state file into this one (for push races)"
    )
    merge.add_argument("other")
    merge.set_defaults(func=cmd_merge)

    test = sub.add_parser("test-alert", help="send a test notification")
    test.add_argument("--console", action="store_true")
    test.set_defaults(func=cmd_test_alert)

    probe = sub.add_parser(
        "probe", help="sample fares across a lead-time range (read-only)"
    )
    probe.add_argument("--min-days", type=int, required=True)
    probe.add_argument("--window", type=int, default=30, help="span of departure dates")
    probe.add_argument("--pairs", type=int, default=15)
    probe.add_argument("--nights", type=int, nargs="+")
    probe.add_argument("--carry-on", type=int, dest="carry_on")
    probe.add_argument("--max-stops", type=int, dest="max_stops")
    probe.add_argument("--to")
    probe.set_defaults(func=cmd_probe)

    dump = sub.add_parser(
        "dump", help="cross-check flight numbers against itineraries (diagnostic)"
    )
    dump.add_argument("--attempts", type=int, default=6)
    dump.add_argument("--to", help="override destination (MID or IATA code)")
    dump.add_argument("--from", dest="from_", help="override origin")
    dump.set_defaults(func=cmd_dump)

    grid = sub.add_parser("grid", help="calendar scans vs full searches (diagnostic)")
    grid.add_argument("--dest")
    grid.add_argument("--nights", type=int)
    grid.add_argument("--limit", type=int)
    grid.set_defaults(func=cmd_grid)

    verify = sub.add_parser("verify", help="one live query, sanity-checked")
    verify.add_argument("--backend", choices=["pairs", "grid", "mock"])
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
