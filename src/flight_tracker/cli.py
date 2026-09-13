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
import sys
from typing import List, Optional

from . import analysis, deadman
from .config import Config, load_config
from .notify import Message, Notifier
from .sources import ScrapeError, get_source
from .sources.base import Offer, date_pairs
from .state import State, utcnow

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
    lines = [
        "%s -> %s roundtrip, %s"
        % (cfg.route.origin_label, cfg.route.destination_label, offer.label),
        "%s out, %s back (%d nights)"
        % (_pretty_date(offer.out_date), _pretty_date(offer.ret_date), offer.nights),
        "Airline: %s" % airlines,
        "Why: %s" % (describe(deal, cfg) or "cheapest in this sweep"),
    ]
    if deal.baseline:
        lines.append(
            "Recent median for these dates: %s"
            % _money(deal.baseline, offer.currency)
        )
    return Message(
        title="%s %s roundtrip %s-%s"
        % (
            "✈️",
            _money(offer.price, offer.currency),
            cfg.route.origin_label,
            cfg.route.destination_label,
        ),
        body="\n".join(lines),
        url=offer.url,
        urgent=True,
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
        cfg.history.days, now=now, max_points_per_pair=cfg.history.max_points_per_pair
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
        title="Cheapest %s -> %s days"
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
    cfg.source.bands = []

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
        "probing %d-%d days out: %d of %d pairs, nights=%s"
        % (args.min_days, args.min_days + args.window, len(sampled), len(pairs),
           cfg.search.trip_nights)
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


def cheapest_report(state: State, cfg: Config, limit: int = 10) -> str:
    """Top N cheapest known fares.

    Ordered by what you decide on, in the order you decide it: price, then
    when it leaves, then which airports, then who's flying it. Two lines per
    fare so it stays readable on a phone.
    """
    rows = []
    for key, snap in state.latest.items():
        try:
            price = float(snap["price"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((price, key, snap))
    rows.sort(key=lambda r: r[0])

    if not rows:
        return "No fares tracked yet."

    lines = ["```"]
    for index, (price, _key, snap) in enumerate(rows[:limit], start=1):
        out_date = str(snap.get("out_date", ""))
        ret_date = str(snap.get("ret_date", ""))
        nights = ""
        if out_date and ret_date:
            nights = " %dn" % (
                dt.date.fromisoformat(ret_date) - dt.date.fromisoformat(out_date)
            ).days

        stops = snap.get("stops")
        stops_text = "nonstop" if stops == 0 else (
            "%s stop" % stops if isinstance(stops, int) else "? stops"
        )
        route = "%s>%s" % (
            snap.get("dep_airport") or "???",
            snap.get("arr_airport") or "???",
        )
        airlines = snap.get("airlines") or []
        airline = ", ".join(str(a) for a in airlines)[:18] or "?"

        lines.append(
            "%2d. %-6s %s -> %s%s"
            % (
                index,
                _money(price, cfg.search.currency),
                _short_date(out_date) if out_date else "?",
                _short_date(ret_date) if ret_date else "?",
                nights,
            )
        )
        lines.append(
            "    %-6s %s  %s  %s"
            % (_clock12(snap.get("dep_time")), route, stops_text, airline)
        )
    lines.append("```")
    lines.append("Outbound times/airports; prices as last seen.")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(args.state)
    message = Message(
        title="\u2708\ufe0f Cheapest %s \u2192 %s, top %d"
        % (cfg.route.origin_label, cfg.route.destination_label, args.limit),
        body=cheapest_report(state, cfg, limit=args.limit),
    )
    if args.send:
        results = Notifier.from_env().send(message)
        for name, ok, error in results:
            print("  %-8s %s%s" % (name, "ok" if ok else "FAILED", "" if ok else ": %s" % error))
        return 0 if all(ok for _, ok, _ in results) else 1
    print(message.as_text())
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Print one raw flight segment from Google's payload, index by index.

    Diagnostic only. fast-flights' model exposes a fixed subset of each
    segment; when we want a field it doesn't surface (a flight number, say),
    this is how we find out whether the data is even there.
    """
    cfg = load_config(args.config)
    pairs = date_pairs(cfg)
    out_date, ret_date = pairs[len(pairs) // 2]

    from fast_flights import fetch_flights_html  # noqa: PLC0415

    source = get_source(cfg)
    html = fetch_flights_html(source._query(out_date, ret_date))  # noqa: SLF001

    import json

    from selectolax.lexbor import LexborHTMLParser  # noqa: PLC0415

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    raw = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    payload = json.loads(raw)

    itineraries = payload[3][0]
    if not itineraries:
        print("no itineraries returned")
        return 1

    segment = itineraries[0][0][2][0]
    print("dates: %s -> %s" % (out_date, ret_date))
    print("segment has %d fields\n" % len(segment))
    for index, value in enumerate(segment):
        text = repr(value)
        if len(text) > 110:
            text = text[:110] + "..."
        print("  [%2d] %s" % (index, text))
    return 0


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
    report.add_argument("--limit", type=int, default=10)
    report.add_argument("--send", action="store_true")
    report.set_defaults(func=cmd_report)

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
    probe.set_defaults(func=cmd_probe)

    dump = sub.add_parser("dump", help="print a raw flight segment (diagnostic)")
    dump.set_defaults(func=cmd_dump)

    verify = sub.add_parser("verify", help="one live query, sanity-checked")
    verify.add_argument("--backend", choices=["pairs", "grid", "mock"])
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
