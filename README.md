# Nonstop NY ↔ LA fare watcher

Watches nonstop roundtrips between the NY metro and the LA metro over a rolling
14-day window, and pings you when something is actually cheap. Free to run:
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

### Request budget

The window is 14 days × trip lengths `[3,4,5,6,7]` ≈ **50 date pairs**. Sweeping
all of them every 15 minutes would be ~4,800 requests/day — the kind of volume
that gets you blocked.

Instead each run walks a **rotating slice** (`source.pairs_per_run`, default 12)
and stores a cursor in state, so consecutive runs pick up where the last left
off. At the default 30-minute cron:

| | |
|---|---|
| Requests per run | 12 |
| Requests per day | ~576 |
| Full window covered every | ~2 hours |

Tune `pairs_per_run` and the cron together. A fare you catch 90 minutes late is
still usually bookable; an IP Google has decided to block catches nothing.

---

## Setup

### 1. Push it to GitHub (public repo = free unlimited Actions minutes)

```bash
gh repo create flight-tracker --public --source . --push
```

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

### 4. Turn on Google's own price tracking too

Free, two clicks, and it's a completely independent safety net for the case
where this scraper breaks in a way even the dead man's switch misses.

---

## How "cheap" is decided

Three independent signals, evaluated per date pair:

| Signal | Meaning | Config |
|---|---|---|
| `threshold` | At or under the price you said you'd pay. | `alerts.threshold_usd` |
| `baseline` | ≥15% below *this date pair's* own recent median. Catches a genuine drop on an expensive week. | `deals.pct_below_baseline`, `deals.min_observations` |
| `percentile` | In the bottom 20% of everything logged lately on the route. Catches "this day is just a cheap day." | `deals.cheap_percentile` |

**It alerts on `threshold` alone, or on `baseline` AND `percentile` together.**
Either of the latter two on its own is too trigger-happy: a 15% drop from an
absurd price is still an absurd price, and the bottom 20% of a quiet week is
just Tuesday.

`baseline` only engages once a date pair has `min_observations` (6) readings, and
`percentile` only once the route has 20+ total — until then you'll get threshold
alerts only, which is the correct behavior for a cold start.

A fare is scored *before* the current sweep is written to history, so a fare is
never part of its own baseline.

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
days under the route-wide percentile cutoff. The daily digest workflow pushes
this to your alert channels every morning.

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
each run prevents this) or Actions is down, nothing evaluates and nothing fires.
That's what `digest.yml` is for: a daily message that also functions as a
heartbeat. If the morning digest stops arriving, the tracker itself is dead.

Exit code stays 0 when unhealthy so state still commits; pass `--fail-on-down`
if you'd rather the workflow go red too.

---

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
- **ToS.** Google discourages scraping. Personal use at this volume is low
  practical risk, but it isn't sanctioned. If datacenter IPs get blocked, the
  same code runs on a Raspberry Pi or an Oracle Cloud always-free VM via cron.
- **Actions timing.** Cron is best-effort; runs get delayed 15+ minutes under
  load. Acceptable for fares.
