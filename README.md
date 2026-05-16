# fareTrader

Autonomous Delta Air Lines fare monitor and auto-booker. Watches configured routes and date windows; when a fare drops to or below your price threshold, it books the seat using your Delta eCredits.

**Live demo:** https://fare-trader.vercel.app/ &nbsp;·&nbsp; **GitHub:** https://github.com/DiogenesofAthens/fareTrader

---

## How It Works

Delta allows free cancellation before departure. Every booked seat is a zero-cost option on a premium seat — hold inventory now, decide whether to fly later. Since eCredits are already-spent money (typically from past cancellations), booking with them creates no new financial exposure.

The agent scans Google Flights first, falls back to Kayak, and executes the full booking flow via Playwright browser automation against delta.com.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Agent & API | Python, FastAPI |
| Browser automation | Playwright (Chromium) |
| Database | SQLite |
| Notifications | Pushover |
| Deployment | Vercel (Python runtime) |

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
playwright install chromium
```

**2. Configure environment variables**

```bash
cp .env.example .env   # then fill in your values
```

| Variable | Description |
|---|---|
| `DELTA_USERNAME` | Delta.com login email |
| `DELTA_PASSWORD` | Delta.com password |
| `PUSHOVER_USER_KEY` | Pushover user key (optional) |
| `PUSHOVER_APP_TOKEN` | Pushover app token (optional) |
| `DRY_RUN` | `true` to scan without booking (default) |
| `SCAN_INTERVAL_MINUTES` | How often to scan (default: 120) |
| `DB_PATH` | SQLite database path (default: bookings.db) |

**3. Configure routes**

Edit `config.py` to set your routes, price thresholds, and date windows:

```python
ROUTES = [
    Route("ATL", "CDG", "delta_one", max_price=5000.0, auto_book=True),
    Route("ATL", "LHR", "delta_one", max_price=4000.0, auto_book=False),  # alert only
]

DATE_WINDOWS = [
    DateWindow("2026-06-01", "2026-10-31"),
]
```

Set `auto_book=False` on any route to receive a Pushover alert without actually booking.

---

## Running

```bash
# Dry run — scans fares, logs results, never books
DRY_RUN=true python agent.py --once

# Live — real bookings with real eCredits
DRY_RUN=false python agent.py --once     # single scan
DRY_RUN=false python agent.py            # continuous scheduler

# Web dashboard
uvicorn app:app --reload --port 8000     # http://localhost:8000

# Check held inventory
python agent.py --status

# Mark a booking cancelled in the local DB
python agent.py --cancel ABC123          # also cancel manually on delta.com
```

---

## Architecture

```
config.py    — Routes, date windows, env var loading
scanner.py   — Fare scraper (Google Flights → Kayak fallback)
booker.py    — Playwright automation: login → search → book → capture PNR
db.py        — SQLite: bookings, price_history, scan_log tables
notify.py    — Pushover push alerts
agent.py     — Orchestrator, scheduler, CLI
app.py       — FastAPI web UI and REST API
static/      — Dashboard frontend (vanilla JS + Chart.js)
```

---

## Web Dashboard

The dashboard at `http://localhost:8000` provides:

- Live scan status with a manual trigger button
- Held booking inventory with one-click cancel
- Price history charts by route and date
- Full scan log with trigger counts, booking counts, and error rates
