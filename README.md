# Nonstop NY ↔ LA fare watcher

Watches **5–7 night** roundtrips between the NY metro and the LA metro for
departures **14–105 days out**, and pings you when one is actually cheap. Nonstop under
**$250**, or one layover under **$200**. Free to run:
GitHub Actions on a public repo, a Discord webhook, no paid APIs.

```bash
python -m flight_tracker run          # one sweep + alerting
python -m flight_tracker days         # cheapest departure days seen so far
python -m flight_tracker test-alert   # prove notifications work
python -m flight_tracker verify       # one live query, checked by hand
```

---

## Read this first: the calendar grid doesn't exist

The original plan was built on `fast_flights.get_calendar_grid()` pulling the
whole departure × return price matrix in **one request**. That function is not
in upstream `fast-flights`. Verified against the published 3.1.0 wheel — the
package exports exactly:

```
get_flights, fetch_flights_html, create_query, FlightQuery, Passengers, Query, ResultList
```

Zero hits for `calendar`, `grid`, or `graph` anywhere in the source. It only
exists in forks of a much older v2, unreviewed and unmaintained.

So the sweep is built on what actually ships: **one query per date pair**, using
city MIDs so a single query still covers every airport in both metros. The grid
remains a supported backend (`source.backend: grid`) behind the same interface —
if you vet a fork that provides it, flip one config line and the request budget
below collapses to 1.

`fast-flights` 3.1.0 requires **Python ≥ 3.10**. Your Mac has system Python
3.9.6 only, so the live backends won't run locally without a newer Python. The
`mock` backend and the tests run fine on 3.9.

### Why 7–105 days

Measured on 2026-09-13 across 385 real fares — cheapest nonstop per date pair:

| Lead time | n | min | median |
|---|---|---|---|
| 7–14 days | 35 | $585 | **$625** |
| 14–21 | 35 | $422 | $498 |
| 21–30 | 45 | $387 | $507 |
| 30–45 | 75 | $374 | $409 |
| 45–60 | 49 | $357 | **$391** ← floor |
| 60–75 | 66 | $374 | $419 |
| 75–95 | 80 | $357 | $391 |

Past 90 days it stops being a market at all. A probe of 90–180 days returned
**$409, every single sample, from one airline, for eleven consecutive
samples** (115–150 days out). That's base-fare inventory sitting untouched
until the date gets closer — sweeping it buys nothing.

So: the near end is a last-minute premium, the far end is a wall, and the
money is in **30–75 days out**. Re-measure any time with
`gh workflow run "probe range" -f min_days=105 -f window=60`.

### Which airports

The origin is the NY city MID, which expands to **JFK, LGA and EWR** on its own
— confirmed in the data (103 / 79 / 66 tracked fares).

The LA city MID does **not** do the same. It returns LAX and nothing else:
248 of 248 tracked fares, and 49 of 49 itineraries in a raw payload. So the
rest of the basin is named explicitly in `route.also_check`:

```yaml
also_check: ["BUR", "SNA", "ONT", "LGB"]
also_check_share: 0.2
```

They get a fifth of each run's budget rather than an equal share, because
they're a background scan rather than a deal watch. A direct probe of Burbank
put the cheapest fare at **$533 against ~$370 for LAX** on comparable dates,
every option a two-leg connection, since nobody flies a transcon nonstop into
BUR. The point of the 20% is to find out whether that holds over weeks, cheaply
— not to catch a two-hour flash sale at Ontario.

The cost is real and worth stating: the secondary space is 4× the primary
(every date pair × four airports), so it cycles roughly **every 37 hours**
while the primary cycles every 2.4. If the secondaries ever turn out to be
competitive, raise the share; if they never are, set `also_check: []` and get
the 2.4 back down to 1.9.

Fares into different airports keep **separate price histories** — the history
key is `date|date|airport|stops`, so a Burbank fare can never average into
LAX's baseline or steal its record low.

### Request budget

The repo is public, so **Actions minutes are free and unlimited**. The only
ceiling that matters is how hard you're willing to hit Google.

276 date pairs (92 departure dates × 3 trip lengths), swept 24 at a time on a
flat rotating cursor — every pair gets equal treatment.

| | |
|---|---|
| Cron | every 10 minutes |
| Pairs per run | 24 |
| Pairs per hour | 144 |
| **Full cycle over every date** | **~1.9 hours** |
| Requests per day | ~3,500 (≈1 every 25 s) |

`source.bands` can weight some lead times over others, and it's currently
**off** — there isn't enough history yet to justify a weighting, so nothing is
privileged. Worth noting that equal *shares* across bands would not have been
uniform: the four bands held 24/72/90/90 pairs, so an even split would cycle
the smallest three times for every one pass over the largest. Uniform per date
pair means no bands at all.

An earlier weighting was set from one day of prices and got it wrong — it gave
the 76–105 day range 20% of the budget on the strength of a probe showing a
flat $409 wall, when that wall actually starts around 115 days and 76–105 turns
out to have the *lowest* median of any range. That's the argument for staying
uniform until the data is real.

A fare that stops being re-checked — the window moves past it, or a band
starves — is dropped from the reports after `history.stale_hours` (24 h). A
full cycle is ~3 hours, so anything older than that is something we stopped
tracking, and it must not sit at the top of the report quoting a price that may
no longer exist. Its price history is kept; only its claim to be current goes.

**Coverage is set by pairs per hour, not by how often the cron fires.** Running
every 5 minutes with 8 pairs covers exactly as much ground as every 15 minutes
with 24. The dial that matters is `pairs_per_run` × runs per hour; the cron
interval on its own changes nothing.

Shares come from measured behaviour, not intuition. The first version gave
76–105 days only 20% of the budget on the strength of a probe showing a flat
$409 wall — but that wall starts at ~115 days, not 76, and the 76–105 band
turns out to have the *lowest* median of any band. It now gets a full share.
The near band keeps a toehold rather than a fair share: it's the worst value on
the route, but it's the only place a last-minute mistake fare could appear.

Totals: 24 requests per run, every 15 minutes, **~2,300 per day** — about one
request every 37 seconds. Each request returns the cheapest fare in *both* stop
classes at no extra cost.

If Google ever starts blocking datacenter IPs, the same code runs unchanged on
a Raspberry Pi or an Oracle Cloud always-free VM under plain `cron`:
`PYTHONPATH=src python -m flight_tracker run`.

---

## Setup

### 1. Push it to GitHub (public)

```bash
gh repo create flight-tracker --public --source . --push
```

Public repos get unlimited free Actions minutes, which is what makes a
15-minute cron free. The trade is that your committed price history is
world-readable — it's just fares, but it is public.

### 2. Alerts — Discord is the easy one

Server settings → Integrations → Webhooks → New Webhook → Copy URL. Then add it
as a repo secret:

```bash
gh secret set DISCORD_WEBHOOK_URL
```

No account linking, no API key, no approval, and it push-notifies your phone.
That's the whole setup.

**Email** (optional, or instead). Gmail needs an [App
Password](https://myaccount.google.com/apppasswords) — your normal password will
be rejected:

```bash
gh secret set SMTP_HOST --body "smtp.gmail.com"
gh secret set SMTP_PORT --body "587"
gh secret set SMTP_USER --body "you@gmail.com"
gh secret set SMTP_PASS   # the 16-char app password
gh secret set EMAIL_TO --body "you@gmail.com"
```

**Text messages, free.** Real SMS APIs (Twilio et al.) all cost money. Your
carrier's email-to-SMS gateway doesn't — add the gateway address to `EMAIL_TO`
alongside your email:

| Carrier | Address |
|---|---|
| Verizon | `5551234567@vtext.com` |
| AT&T | `5551234567@txt.att.net` |
| T-Mobile | `5551234567@tmomail.net` |
| Google Fi | `5551234567@msg.fi.google.com` |

Gateways truncate aggressively, so texts get the title and little else. Fine for
"$188, tap the link."

Every channel is optional. With none set, alerts print to stdout and show up in
the Actions log.

```bash
python -m flight_tracker test-alert    # do this before trusting it
```

### 3. Verify the scrape before you trust a single alert

```bash
python -m flight_tracker verify
```

Needs Python ≥ 3.10. If you only have 3.9, run it on Actions instead:
`gh workflow run "verify scrape"`, then read the job log.

This runs one live query and prints the resolved Google Flights URL, the price,
the airline, and the stop count. Two things it's checking:

1. **Are the city MIDs right?** `/m/02_286` (NY) and `/m/030qb3t` (LA) came from
   the research notes and are *unverified*. If `verify` returns nothing or
   nonsense, open the printed URL — it'll show you what Google thinks you asked
   for.
2. **Is `max_stops=0` respected?** Open the URL, set the stops filter to
   "Nonstop only", and confirm the price matches. If the filter is silently
   ignored, every alert will quietly include one-stops and you will not notice
   for weeks. (The parser drops multi-segment legs as a second line of defense,
   but that only works when segment data comes back.)

### 4. Pick a threshold that can actually fire

Two live data points from 2026-09-13, both nonstop NY→LA roundtrips departing
within a week: **$852** and **$957**. That window is the expensive
last-minute one, which is why the search starts 7 days out and runs to 90.

`alerts.threshold_usd` is still at the original **$250**. Whether that's
reachable 1–3 months out is an open question — nobody has data on this route
yet, including this tracker. Give it two or three days, then:

```bash
python -m flight_tracker days
```

Set the threshold just under what you actually see. Too high and it cries wolf;
too low and it never fires and you'll assume it's broken. The `baseline` and
`percentile` signals cover you either way once history builds — they're
relative, so they work without you guessing a number correctly.

### 4. Turn on Google's own price tracking too

Free, two clicks, and it's a completely independent safety net for the case
where this scraper breaks in a way even the dead man's switch misses.

---

## How "cheap" is decided

Three independent signals, evaluated per date pair:

| Signal | Meaning | Alerts? |
|---|---|---|
| `threshold` | Nonstop ≤ **$250**, or one layover ≤ **$200**. | **yes — the only trigger** |
| `percentile` | In the bottom 5% of everything logged lately. | no |
| `baseline` | ≥15% below this date pair's own recent median. | no |

**Only `threshold` sends a message**, and the bar depends on how many stops:

| | Alerts at |
|---|---|
| Nonstop | ≤ `alerts.threshold_usd` — **$250** |
| One layover | ≤ `alerts.threshold_usd_with_stops` — **$200** |

A layover has to earn its place. A $240 connecting fare stays silent; a $240
nonstop doesn't. If the stop count can't be read from the payload, the fare is
treated as connecting — better to stay quiet than to alert on something that
turns out to have a stop in it.

The two fare classes keep **separate price histories** (the connecting one gets
a `|1` suffix on its state key), so a $210 one-stop never averages into the
nonstop baseline for those dates.

The other two still run: they mark cheap days in the `days` report and explain
*why* an alerting fare is good ("$238 — under your $250 threshold; in the
cheapest 5% of fares we've logged"). They just don't wake your phone on their
own, because a relative bargain is still whatever the route happens to cost
that week.

A fare is scored *before* the current sweep is written to history, so it's never
part of its own baseline.

### Low-price days

```
$ python -m flight_tracker days

DEPART         RETURN            PRICE
Sun Sep 20     Sat Sep 26         $241  <- cheap day
Tue Sep 15     Fri Sep 18         $244  <- cheap day
Wed Sep 16     Wed Sep 23         $245  <- cheap day
Thu Sep 24     Mon Sep 28         $310
```

Cheapest fare per departure date, merged across history and the current sweep —
so a rotating partial sweep still shows the whole window. `<- cheap day` marks
days under the route-wide percentile cutoff. Run it any time; nothing pushes it
on a schedule.

### Record lows

A fixed threshold can stay silent for months. If nothing on the route has ever
been under $250, a $250 alarm never rings and the tracker looks dead while it's
working perfectly. So there's a second, quieter alert: **a fare that beats the
cheapest ever seen**, whatever the number.

It can't become a stream, because every alert raises its own bar. Nonstop and
connecting fares keep independent records, the first fare in a class arms the
bar silently (alerting there would mean alerting on the first thing we ever
saw), and an improvement smaller than `alerts.record_min_drop_usd` ($5) is
ignored. Records expire with the history window so one winter fluke doesn't set
the bar forever.

These arrive in blue with no `@here` — they're information, not the $250 alarm.
Turn them off with `alerts.record_low: false`.

### Weekly report

Every **Monday at 6:00am Pacific**, the top 10 cheapest tracked fares land in
Discord, ordered the way you'd actually decide: price, then dates, then
departure time, then airports, then airline.

> **$278**  ·  Wed Oct 21 → Tue Oct 27  ·  6n  ·  LGA→LAX  ·  1 stop  ·  Southwest
>
> **$278**  ·  Wed Nov 4 → Tue Nov 10  ·  6n  ·  LGA→LAX  ·  1 stop  ·  Southwest  ·  +4 more dates
>
> **$409**  ·  Thu Oct 29 → Sun Nov 1  ·  3n  ·  JFK→LAX  ·  nonstop  ·  American

One fare per line. The **cheapest** fares are selected, then listed in
**departure order** — ranking by price is what makes the list worth reading,
reading it in date order is what makes it usable for planning a trip.

**The price is a link** — tapping it reopens the exact Google Flights search
that found that fare, with the dates, stop limit and bag filters already
applied. A flight number is a poor handle on a fare three months out: schedules
shift, and the number alone won't reconstruct the search. Masked links render
inside embeds, which is one more reason the report is an embed rather than a
plain message. If the links would push the description past Discord's 4096
character cap, they're dropped wholesale rather than truncated mid-URL —
better a linkless row than a row cut in half.

**It's normal text, not a fixed-width table.** A monospace table is only
readable while it fits the reader's window — Discord wraps at about 82
characters, and past that it breaks every row mid-cell, destroying the exact
alignment that justified the monospace font in the first place. Normal text
reflows at a separator instead, so a narrow window costs a wrapped line rather
than a mangled grid.

**The price is the link.** Tapping it reopens the exact Google Flights search
that found the fare, with the dates, stop limit and bag filters already
applied. That's the only link on the row: a flight number is a poor handle on a
fare months out — schedules shift and the number alone won't reconstruct the
search — so the row names the airline and lets the link do the finding. Masked
links render inside embeds, which is one more reason the report is an embed
rather than a plain message. If the links would push the description past
Discord's 4096 character cap they're dropped wholesale rather than truncated
mid-URL.

**It's normal text, not a fixed-width table.** A monospace table is only
readable while it fits the reader's window — Discord wraps at about 82
characters, and past that it breaks every row mid-cell, destroying the exact
alignment that justified the monospace font in the first place. Normal text
reflows at a separator instead, so a narrow window costs a wrapped line rather
than a mangled grid.

**Two links per fare.** The price reopens the Google Flights search that found
it. The flight number links straight to the airline's own booking search, dates
prefilled — but only for carriers whose deep link was opened in a browser and
confirmed to work: **Southwest, Delta, Frontier**. American and JetBlue answer
automated requests with a bot challenge so their format couldn't be verified,
and Alaska's parameters were confirmed *not* to work — it loads the form and
reports the dates as missing. Those carriers get no airline link rather than a
broken one. Airlines change these formats without warning, so treat a link that
stops working as wear, not a surprise.

Any time, on demand:

```bash
python -m flight_tracker report --limit 10
```

Alert embeds put the deeplink on the embed's own `url`, so the **title itself
is tappable** — no 400 characters of base64 in the message body, and no URL
shortener service. Email and SMS still get the raw URL.

Two things this report is honest about. Times and airports describe the
**outbound leg only** — Google's roundtrip payload prices the whole trip but
only details the outbound, so the return leg's airports and times aren't in the
response at all. And prices are **as last seen**, which for the far bands can
be several hours old.

GitHub cron is UTC-only and DST-blind, so the workflow fires at both 13:00 and
14:00 UTC and the job itself checks whether it's really 6am in Los Angeles.
Without that the report would drift an hour twice a year.

### Running a scan on demand

```bash
./scan
```

Sweeps 40 date pairs immediately. Pass a number for more: `./scan 120` for a
deep scan across most of the horizon.

The script runs the scan locally if you have a Python ≥ 3.10 with the scraper
installed (see below), and otherwise dispatches the GitHub Actions workflow,
waits for it, and prints the result. Either way it's the same code path as the
cron, so alerts fire normally.

The raw equivalents, if you'd rather:

```bash
gh workflow run "watch fares" -f pairs=80
```

You can also hit **Run workflow** on the
[watch fares](https://github.com/Joe-433/flight-tracker/actions/workflows/watch.yml)
page — useful from a phone.

**To make `./scan` run locally** (instant, no GitHub round trip) you need a
Python ≥ 3.10, which macOS doesn't ship:

```bash
brew install python@3.12 && python3.12 -m venv .venv-live && .venv-live/bin/pip install -r requirements.txt
```

### Alert spam control

Dedup is per date pair: once alerted, a pair stays quiet for
`alerts.cooldown_hours` (12) — **unless** it drops another
`alerts.rebeat_drop_usd` ($15), because a fare that keeps falling is worth
hearing about twice. Hard cap of `alerts.max_per_run` (3) per sweep.

---

## Dead man's switch

The real failure mode isn't a crash. It's the scraper quietly returning zero
results forever while the workflow stays green and you assume fares are just
high. Silence is treated as an alertable event.

Two triggers, either one fires:

- **`deadman.fail_runs`** (2) consecutive sweeps returned no usable fare, or
- **`deadman.stale_hours`** (6) hours have passed with no usable fare at all.

It alerts once, then nags at most every `renotify_hours` (24) while still down,
and sends an explicit **"✅ back"** when fares return — so an all-clear is never
ambiguous.

**The gap it can't cover:** both triggers need a run to actually happen. If
GitHub disables the cron (60 days of repo inactivity — committing state back
each run prevents this) or Actions is down, nothing evaluates and nothing
fires. The Monday report is the canary for that: if it stops arriving, the
tracker itself has stopped, not the fares.

A daily digest used to fill that role, but it fired at the same hour as the
Monday report and delivered two messages on Mondays. Weekly detection of a
total outage is a fair price for not being messaged about the same fares twice.
The repo itself is the faster signal anyway — `data/state.json` should carry a
new commit every ten minutes.

Exit code stays 0 when unhealthy so state still commits; pass `--fail-on-down`
if you'd rather the workflow go red too.

---

### Concurrent runs

`data/state.json` is a generated document that every run rewrites in full, so
`git pull --rebase` can only ever conflict on it — and a failed rebase leaves
the checkout on a detached HEAD that can't be pushed. That took a run down on
2026-09-14.

The commit step rebases nothing now. It resets to the remote, merges the remote
state into its own **semantically**, and pushes that, retrying five times with
backoff if it loses another race. The merge is well defined because the data
says what the winner should be:

| Field | Winner |
|---|---|
| observations | union, de-duplicated by timestamp, kept in time order |
| latest | freshest snapshot — it's what the reports render |
| alerts | most recent, so a cooldown is never silently reset |
| records | lowest price, because a record low is a fact about the route |
| cursors | furthest, so the losing run's ground isn't re-swept |
| failure streak | a success anywhere clears it |

## Layout

```
src/flight_tracker/
  sources/          EVERY call that touches Google lives here
    base.py           Offer, date-pair math, rotating slice
    pairs.py          one query per date pair  (works today)
    grid.py           one query for the matrix (needs a fork)
    mock.py           deterministic fakes, no network
  analysis.py       threshold / baseline / percentile scoring, cheap days
  state.py          JSON history, compaction, alert dedup
  deadman.py        health counters
  notify.py         Discord + SMTP/SMS, stdlib only
  cli.py            run / days / test-alert / verify
```

The scraping boundary is the whole point of that structure. When Google changes
something — and it will — `sources/` is the only directory that should need to
change. Everything else is tested offline against `mock`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m flight_tracker --state data/dev.local.json \
    run --backend mock --console
```

`FT_MOCK_EMPTY=1` makes the mock backend return nothing, which is how you
exercise the dead man's switch. `FT_MOCK_SEED=n` shifts prices to simulate
movement between runs.

## Notes

- **LGA perimeter rule.** LaGuardia's 1,500-mile limit means LGA–LA nonstops are
  largely Saturday-only. City-MID search plus the nonstop filter handles this,
  but it explains sparse results.
- **Partial sweep errors are normal.** A date pair with no nonstops at all
  errors rather than returning zero. The run prints each one; a handful per
  sweep is expected, all of them failing is what the dead man's switch is for.
- **ToS.** Google discourages scraping. Personal use at this volume is low
  practical risk, but it isn't sanctioned. If datacenter IPs get blocked, the
  same code runs on a Raspberry Pi or an Oracle Cloud always-free VM via cron —
  which also gets you off the Actions-minute meter entirely.
- **Actions timing.** Cron is best-effort; runs get delayed 15+ minutes under
  load. Acceptable for fares.
