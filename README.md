# Nonstop NY ↔ LA fare watcher

Watches **5–7 night** roundtrips from **JFK and LGA** to **LAX, BUR, SNA, ONT and
LGB**, departing **14–105 days out**, and pings you when one is actually cheap:
nonstop under **$250**, or one layover under **$200**. Free to run: GitHub
Actions on a public repo, a Discord webhook, no paid APIs.

```bash
./scan                                   # run a scan now
python -m flight_tracker report          # cheapest 3 nonstop + 3 one-stop
python -m flight_tracker grid            # calendar scans vs full searches
python -m flight_tracker test-alert      # prove notifications work
python -m flight_tracker verify          # one live query, checked by hand
```

---

## How it works

The search space is **1,380 trips**: 92 departure dates × 3 trip lengths × 5
airports. Searching each one fully costs a request, and one request per trip
per run is exactly what gets an IP blocked. So a run has three stages:

```
plan    calendar scans ── 60 requests price all 1,380 trips, approximately
          │
          └─ planner ──── picks the 60 trips most worth a full search
                  │
drill   ┌─────────┼─────────┬─────────┐
        runner 1  runner 2  runner 3  runner 4    15 full searches each,
        (own IP)  (own IP)  (own IP)  (own IP)    exact prices + flight codes
        └─────────┴────┬────┴─────────┘
apply           fold results in, alert, commit
```

Every trip gets a fresh approximate price on every run, and the exact searches
go where prices are low or moving. A run takes about three and a half minutes.

### The calendar grid

Google Flights' calendar view is served by an internal call, `GetCalendarGraph`,
that returns the cheapest round-trip price for **every departure date in a
61-day window in one request**. The original plan was built on it and then
shelved, because the `fast-flights` package this tracker uses for full searches
doesn't expose it.

The [`flights`](https://github.com/punitarani/fli) package does. Measured live
on 2026-09-23, from GitHub Actions:

| | |
|---|---|
| Scans (5 airports × 3 lengths × 2 stop classes) | 30 |
| Requests | 60 (two 61-day windows each) |
| Failures | 0 |
| Time | ~75 seconds |
| Agreement with full searches, same trips | 82 of 187 within $15; median −$17 |

The calendar runs a little **below** full searches, and its biggest misses are
near-term dates where prices move within hours. It's a first pass, not the
source of truth: it decides where full searches go, and never sends an alert
by itself.

### Adaptive scheduling

Each trip earns a full search on an interval set by what's known about it,
and the most overdue trips go first (`src/flight_tracker/planner.py`):

| Trip | Searched every |
|---|---|
| Under an alert line, or would beat the record low | 15 min |
| Cheapest 5% of its stop class, or the calendar moved | 1 hour |
| Cheapest 20% | 3 hours |
| Everything else | 24 hours |

Nothing starves. Overdue is *time since last search ÷ interval*, so a dull
trip's number keeps climbing until it outranks cheap ones that were just
checked. When there are more trips due than slots in a run, everything runs
proportionally late rather than some trips never running.

**Movement is measured calendar-to-calendar.** Comparing the calendar to the
last full search directly would be wrong, because the calendar sits a median
$17 lower. Dozens of trips would read as "dropped" permanently and get
re-searched every hour forever. Instead every full search records what the
calendar said at that moment, and a trip has moved when the calendar has
fallen since then. The two readings share the same bias, so it cancels. The
best price estimate is the last full search, shifted by however far the
calendar has moved since.

If the calendar stops working, the planner falls back to scheduling from
full-search prices alone. The sweep keeps going, and after three failed runs
you get a heads-up in Discord ("Calendar scans are failing"), not the dead
man's switch.

### Carry-on fees are not in these prices

Your spec said carry-on only. `fast-flights` sends a carry-on bag filter with
every search, and it has **no measurable effect** on the prices returned:
identical with and without. The calendar *does* honour it. Turned on, the
calendar ran a median **$90 above** full searches on the same trips, which is
about a round trip of carry-on fees on basic fares. So both sources are set to
price without bags (`grid.include_bags: false`), and every price here is a
base fare, the same thing a default Google Flights search shows.

That matters for basic-economy fares that charge for a carry-on. Southwest,
which keeps producing the cheapest fares on this route, includes one free.

### Why 14–105 days

Measured on 2026-09-13 across 385 real fares, cheapest nonstop per date pair:

| Lead time | n | min | median |
|---|---|---|---|
| 7–14 days | 35 | $585 | **$625** |
| 14–21 | 35 | $422 | $498 |
| 21–30 | 45 | $387 | $507 |
| 30–45 | 75 | $374 | $409 |
| 45–60 | 49 | $357 | **$391** ← floor |
| 60–75 | 66 | $374 | $419 |
| 75–95 | 80 | $357 | $391 |

Past about 105 days it stops being a market. A probe of 90–180 days returned
**$409 from one airline for eleven consecutive samples** (115–150 days out):
base-fare inventory sitting untouched until the date gets closer. The near
end is a last-minute premium. The first two weeks are excluded entirely.

### Which airports

**Origins:** the NY city MID covers JFK, LGA and EWR in one full search. EWR is
excluded (`route.exclude_origins`), and its itineraries are dropped from the
results so the cheapest JFK/LGA option still wins. The calendar can't take a
MID, so it's given JFK and LGA explicitly.

**Destinations** are named one by one. The LA city MID returns LAX and nothing
else (248 of 248 tracked fares, 49 of 49 itineraries in a raw payload), so the
basin has to be spelled out. It was worth it. An early six-fare sample made the
secondary airports look $200 dearer; a week of data said the opposite:

| Airport | Cheapest (2026-09-22) |
|---|---|
| ONT | $230 |
| BUR | $239 |
| SNA | $272 |
| LAX | $278 |
| LGB | $332 |

Fares into different airports keep separate price histories. The history key
is `date|date|airport|stops`, so a Burbank fare can never average into LAX's
baseline or steal its record low.

### Request budget

Two things bound the sweep.

**GitHub throttles its own schedules, hard.** A `*/10` cron is nominally 144
runs a day. It delivered 41, then 12, then 6, degrading over a week. Scheduled
workflows are best-effort, and GitHub deprioritises repos that ask for a lot.
That's why the primary trigger is external (see Setup): `workflow_dispatch`
events run when they're sent. The built-in schedule remains as a backstop
every two hours.

**Google is the real ceiling**, and nobody publishes it. At a 30-minute
external trigger:

| | Per run | Per day |
|---|---|---|
| Calendar requests (skipped if <25 min old) | 60 | ≤2,880 |
| Full searches, split across 4 runners | 60 | 2,880 |
| Machines (and usually IPs) | 5 | 240 |

Each runner makes about 15 requests and then disappears. That's a far gentler
pattern per IP than the old single-runner sweeps of 120.

Rough demand, if every trip were searched exactly on its interval: ~4,400 full
searches a day. Supply is 2,880, so the schedule runs about 1.5× slower than
the table above, spread proportionally. Raise `source.drills_per_run` to 90 to
meet it exactly, at the cost of more traffic.

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

### 3. External trigger — every 30 minutes

GitHub's own schedule can't be relied on (see Request budget), so a free
external cron service calls the workflow's `workflow_dispatch` endpoint
instead. Two parts, both of which need your accounts:

**A GitHub token that can only start workflows.** GitHub → Settings → Developer
settings → Personal access tokens → **Fine-grained tokens** → Generate new
token:

| Field | Value |
|---|---|
| Expiration | 1 year (set a calendar reminder) |
| Repository access | Only select repositories → `Joe-433/flight-tracker` |
| Repository permissions → **Actions** | **Read and write** |
| Everything else | No access |

That token can start, cancel and re-run this repo's workflows. It can't read
or change code, and it can't see secrets. If it leaks, the worst case is
someone triggering extra scans.

**A cron job that calls GitHub.** On [cron-job.org](https://cron-job.org)
(free), create a job:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/Joe-433/flight-tracker/actions/workflows/watch.yml/dispatches` |
| Schedule | Every 30 minutes |
| Advanced → Request method | `POST` |
| Advanced → Headers | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <your token>`<br>`X-GitHub-Api-Version: 2022-11-28`<br>`Content-Type: application/json` |
| Advanced → Request body | `{"ref":"main"}` |

Save it and press **Test run**. The answer should be **HTTP 204**, and a
`watch fares` run should appear within seconds:

```bash
gh run list --workflow="watch fares" --event workflow_dispatch --limit 3
```

That exact request was verified against this repo on 2026-09-23: 204, with a
run started 12 seconds later.

**How you'll know it's working:** the Monday report's footer reads
`N runs in the last 24h`. With the trigger healthy that's about 48. If it falls
toward 12, the trigger has stopped (an expired token, most likely) and only
the two-hour backstop is running.

### 4. Verify the scrape before you trust a single alert

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

### 5. Pick a threshold that can actually fire

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

### 6. Turn on Google's own price tracking too

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

Cheapest fare per departure date, merged across history and the current run,
so a run that only fully searched some trips still shows the whole window. `<- cheap day` marks
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

> **Nonstop**
> **$357**  ·  Wed Nov 4 → Tue Nov 10  ·  6n  ·  EWR→LAX  ·  nonstop  ·  Alaska  ·  +3 more dates
> **$367**  ·  Sat Dec 5 → Sat Dec 12  ·  7n  ·  EWR→LAX  ·  nonstop  ·  Alaska
>
> **One layover**
> **$230**  ·  Wed Nov 4 → Mon Nov 9  ·  5n  ·  LGA→ONT  ·  1 stop  ·  Southwest  ·  +2 more dates
> **$230**  ·  Thu Dec 3 → Wed Dec 9  ·  6n  ·  LGA→ONT  ·  1 stop  ·  Southwest

**Two sections, three fares each.** Connecting fares are reliably cheaper on
this route, so a single ranked list handed them every slot and the nonstops —
the reason the tracker exists — never appeared at all. Splitting them keeps the
cheap connection visible without letting it hide what a direct flight costs.

Within each section the **cheapest** fares are selected, then listed in
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
response at all. And prices are **as last seen**. For routine trips that can
be most of a day, which is why each row says how old its price is once it
passes twelve hours.

GitHub cron is UTC-only and DST-blind, so the workflow fires at both 13:00 and
14:00 UTC and the job itself checks whether it's really 6am in Los Angeles.
Without that the report would drift an hour twice a year.

### Running a scan on demand

```bash
./scan            # calendar scans + the configured 60 full searches
./scan 200        # a deep scan: 200 full searches across the 4 runners
```

The script runs locally if you have Python ≥ 3.10 with the dependencies
installed. Otherwise it dispatches the workflow, waits, and prints the result.
Either way it's the same code path as the scheduled runs, so alerts fire
normally. From a phone, the **Run workflow** button on the
[watch fares](https://github.com/Joe-433/flight-tracker/actions/workflows/watch.yml)
page does the same thing, with switches for alerts and calendar scans.

To make `./scan` run locally, you need a Python macOS doesn't ship:

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
| checked | latest search time, so the planner never re-spends a slot the other run just used |
| grid | freshest calendar scan, per scan |
| runs | union |
| failure streaks | a success anywhere clears them |

After merging, the result is **pruned again** with the current config. The
remote copy still holds everything this run just pruned, and a union can't
tell "data you don't have" apart from "data you deliberately deleted". Without
the re-prune, every stale fare was resurrected on every run. That's how
eight-day-old prices ended up in the report.

## Layout

```
src/flight_tracker/
  pipeline.py       a run in three stages: plan, drill, apply
  planner.py        which trips earn a full search this run
  sources/          EVERY call that touches Google lives here
    grid.py           calendar scans: all dates, one request per window
    pairs.py          full searches: one trip, exact price and flights
    mock.py           deterministic fakes, no network
    base.py           Offer, date-pair math
  analysis.py       threshold / baseline / percentile scoring, record lows
  state.py          JSON history, merging, pruning, alert dedup
  deadman.py        health counters
  notify.py         Discord embeds + SMTP/SMS, stdlib only
  booking.py        airline deep links, where verified
  cli.py            plan / sweep / apply / run / report / grid / verify / ...
```

`sources/` is the scraping boundary. When Google changes something (and it
will), that directory is the only one that should need to change. Everything
else is tested offline against the mocks.

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
