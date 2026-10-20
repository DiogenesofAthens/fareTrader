# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

fareTrader is a Python agent that monitors Delta Air Lines first-class and Delta One fares across configured routes and date windows. When a fare hits or drops below a configured price threshold, the agent automatically books it using the user's Delta eCredits.

**The free-options strategy:** Delta allows free cancellation before departure. Every booked seat is a zero-cost option on a premium seat — the agent holds inventory and the user cancels anything they decide not to fly. Net cost is $0 unless you actually travel.

**Why eCredits:** eCredits are already "spent" money (from previous trip cancellations). Using a credit card would create real financial exposure; eCredits do not.

## Commands

```bash
# Install dependencies
pip install requests beautifulsoup4 playwright
playwright install chromium

# Dry-run scan (safe — will not buy anything)
DRY_RUN=true python3 agent.py --once

# Go live (real bookings with real eCredits)
DRY_RUN=false python3 agent.py --once     # single scan
DRY_RUN=false python3 agent.py            # continuous scheduler

# Inspect held inventory
python3 agent.py --status

# Mark a booking cancelled in the local DB
python3 agent.py --cancel ABC123

# Run tests
python3 test_fixes.py
```

> **`--cancel` warning:** Updates only the local DB. You must also cancel on delta.com or by phone.

## Architecture

```
config.py    — All constants, route definitions, env var loading.
scanner.py   — Price scraper: Google Flights first, Kayak fallback. Returns Flight objects.
booker.py    — Playwright automation against delta.com: login → search → book → capture PNR.
db.py        — SQLite layer (bookings.db). Three tables: bookings, price_history, scan_log.
notify.py    — Pushover push alerts. No-op if PUSHOVER_* env vars are absent.
agent.py     — Orchestrator + scheduler + CLI. Ties everything together.
```

**Core scan loop** (`agent.run_scan()`): for each `Route × date`, the agent:
1. Calls `scanner.get_price()` → records result to `db.insert_price()`
2. If price ≤ `route.max_price`, fires `notify.price_trigger_found()`
3. If `route.auto_book` is True, the slot isn't already held and the per-route limit isn't hit, calls `booker.book_flight()`. If `DELTA_USERNAME`/`DELTA_PASSWORD` aren't set (as in the CI demo scan), it logs "Would auto-book … (no Delta credentials configured)" at INFO instead and skips the booker; the price and trigger are still recorded and neither `error_count` nor `booking_count` changes.
4. On success, calls `db.insert_booking()` + `notify.booking_confirmed()`. A failed booking attempt (with credentials set) counts as an error.

After all routes, `agent._check_cancel_deadlines()` fires `notify.cancel_deadline_warning()` for any held booking within 7 days of its cancellation deadline.

**Booking limit guard:** `MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN = 1` (hardcoded in `config.py`, not an env var). When this limit is hit for a route, subsequent dates on that route skip the booking step but still have their prices recorded and trigger notifications sent.

**Booking flow** (`booker._run_booking_flow()`): 10 steps — login → search (with passenger count if `seats > 1`) → cabin filter → select first result → skip seat selection → skip passenger info → apply eCredits → **check remaining eCredit balance ≥ `MIN_ECREDIT_BUFFER_USD`** → purchase → capture PNR. `DRY_RUN=True` stops before the purchase click and returns a fake PNR.

**Price scraping** (`scanner.get_price()`): Google Flights first, then Kayak as a fallback. Google's URL is a `tfs` protobuf carrying the cabin (seat field 9) and a Delta-only airline filter (leg field 6 = `DL`). The parser reads only the per-itinerary `aria-label`s ("From 9334 US dollars. Nonstop flight with Delta. …") and keeps Delta-marketed, Delta-operated, single-cabin itineraries between the cabin floor (`_min_plausible`) and `$20,000`. Kayak counts only `data-price` attributes (`$25,000` cap); its HTML is client-rendered, so it rarely returns anything. Unattributable numbers (JSON blobs, `$` amounts) are never used: in Aug–Oct 2026 they recorded other airlines' and mixed-cabin fares and tripped the quarantine on nearly every scan. Applies `REQUEST_DELAY_SECONDS = 3.0` after every request.

**Dates scanned:** Only Fridays and Saturdays within each window in `config.DATE_WINDOWS`. The default is a `RollingDateWindow(SCAN_HORIZON_START_DAYS=14, SCAN_HORIZON_END_DAYS=126)`, i.e. 2 to 18 weeks after the scan date: about 33 dates per route, ~130 lookups per scan. It is recomputed on every read of `.start`/`.end`, so it never expires (the old fixed Jun–Oct 2026 window left nothing to scan after Oct 31). A fixed `DateWindow("YYYY-MM-DD", "YYYY-MM-DD")` still works alongside it. Don't widen the horizon past ~144 lookups per scan, the largest size proven in CI against the 30-minute job timeout; `test_scan_size_within_ci_budget` enforces this.

**Routes:** ATL→CDG, ATL→HND, ATL→LHR (alert only), JFK→MXP, all Delta One. Tokyo is Haneda: Delta moved all its Tokyo flights from Narita to Haneda in 2020, and ATL→NRT only ever returned Korean Air-operated connections, which the scanner rejects. A route to an airport Delta doesn't operate will never price. The snapshot export leaves out rows for routes no longer in `config.ROUTES`; they stay in the database.

**Thresholds (`max_price`)** were set for summer fares and haven't been recalibrated for the rolling window. Winter Delta One fares come in cheap buckets that sit under most of them: the Oct 20, 2026 scan triggered 57 of 128 dates (ATL–HND's everyday fare is $6,211 against $6,500). Recalibrate from the latest scan's per-route prices, and keep the scan-wide trigger rate well below `QUARANTINE_TRIGGER_RATE` (0.9).

## Environment variables

### Required for booking
```bash
export DELTA_USERNAME="your_skymiles_number_or_email"
export DELTA_PASSWORD="your_delta_password"
```

### Optional (safe defaults)
```bash
export DRY_RUN=true                  # default: true
export SCAN_INTERVAL_MINUTES=120     # default: 120; never go below 60
export MIN_ECREDIT_BUFFER_USD=500    # default: 500
export DB_PATH=bookings.db           # default: bookings.db in cwd
export LOG_LEVEL=INFO                # default: INFO
export PUSHOVER_USER_KEY="..."       # alerts silent without these
export PUSHOVER_APP_TOKEN="..."
export SCREENSHOT_DIR="."            # where error_*.png files land
```

## The fragile parts

delta.com and Google Flights change their HTML periodically. Broken selectors are almost always the cause when bookings or price lookups start failing.

- **Booking selectors:** All live in the `_Sel` class in `booker.py` (~line 40). Every form field, button, and confirmation element is a class attribute there.
- **Price scraping selectors:** `_scrape_google_flights()` and `_scrape_kayak()` in `scanner.py`. The `_GF_*_PATTERN` label regexes and the Kayak `data-price` lookup are the first things to check. A route that suddenly returns no price usually means Google changed the label wording, which is safer than the old failure mode of recording a wrong fare.
- **Error screenshots** are saved to `SCREENSHOT_DIR` (default: project root) as `error_<label>_<timestamp>.png` on any Playwright failure. Always check these first.

### Debugging workflow

1. Check `error_*.png` in the project root.
2. Open delta.com in a browser, reproduce the step manually, inspect the element.
3. Update the selector in `_Sel` (booker.py) or the scraping function (scanner.py).
4. Re-run with `--once` in dry-run mode.

## Do not change

- **Never hardcode credentials.** Always `os.getenv()` in `config.py`.
- **Never set `DRY_RUN=False` unless explicitly asked.** A bug in the booking flow could drain eCredits.
- **Never decrease `SCAN_INTERVAL_MINUTES` below 60.** Risks rate-limiting or IP blocks.
- **Never remove the `db.is_already_booked()` guard in `agent.py`.** This prevents double-booking the same route + date + cabin.
- **Never remove the eCredit balance check in `booker._run_booking_flow()`.** This is what enforces `MIN_ECREDIT_BUFFER_USD`.

## Database

SQLite file: `bookings.db` (auto-created on first run by `db.init_db()`).

### Useful queries

```sql
-- All held bookings
SELECT pnr, origin||'→'||destination AS route, cabin, travel_date, price_usd, cancel_by
FROM bookings WHERE status='held' ORDER BY travel_date;

-- Price history for a route
SELECT travel_date, price_usd, source, scanned_at
FROM price_history WHERE origin='ATL' AND destination='CDG'
ORDER BY scanned_at DESC LIMIT 50;

-- Cheapest price seen per date
SELECT travel_date, MIN(price_usd) AS min_price, MAX(price_usd) AS max_price, COUNT(*) AS observations
FROM price_history WHERE origin='ATL' AND destination='CDG'
GROUP BY travel_date ORDER BY travel_date;

-- Recent scan log
SELECT started_at, routes_checked, dates_checked, trigger_count, booking_count, error_count, dry_run
FROM scan_log ORDER BY started_at DESC LIMIT 20;
```

```bash
sqlite3 bookings.db "SELECT * FROM bookings WHERE status='held';"
```
