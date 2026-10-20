"""
Central configuration. All secrets come from environment variables.
Edit ROUTES and DATE_WINDOWS directly; everything else via env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Union

# ---------------------------------------------------------------------------
# Route definitions
# ---------------------------------------------------------------------------

@dataclass
class Route:
    origin: str
    destination: str
    cabin: str          # "first" | "business" | "delta_one"
    max_price: float    # USD threshold — book at or below this
    auto_book: bool     # False → alert only, never purchase
    seats: int = 1      # seats per booking attempt

# The scanner keeps only Delta-operated itineraries, so a route to an airport
# Delta doesn't serve never prices. Tokyo is Haneda: Delta moved all its Tokyo
# flights there from Narita in 2020 and flies ATL–HND nonstop. ATL→NRT only
# returned Korean Air-operated connections, which the scanner rejects.
ROUTES: list[Route] = [
    Route("ATL", "CDG", "delta_one", 5000.0, auto_book=True),
    Route("ATL", "HND", "delta_one", 6500.0, auto_book=True),
    Route("ATL", "LHR", "delta_one", 4000.0, auto_book=False),  # alert only
    Route("JFK", "MXP", "delta_one", 4200.0, auto_book=True),
]

# ---------------------------------------------------------------------------
# Date windows — agent scans Fridays & Saturdays within each window
# ---------------------------------------------------------------------------

@dataclass
class DateWindow:
    """Fixed calendar window."""
    start: str   # YYYY-MM-DD inclusive
    end: str     # YYYY-MM-DD inclusive

    def resolve(self, today: Optional[date] = None) -> DateWindow:
        return self


@dataclass
class RollingDateWindow:
    """Window that moves with the calendar: start_days to end_days after the
    scan date, both inclusive.

    Exposes the same .start / .end ISO strings as DateWindow, recomputed on
    every read, so the scheduler, the snapshot export and the API all see
    today's window. A fixed window silently stops scanning once its end date
    passes (the Jun–Oct 2026 window would have left nothing after Oct 31).
    """
    start_days: int
    end_days: int

    def resolve(self, today: Optional[date] = None) -> DateWindow:
        base = today or date.today()
        return DateWindow(
            start=(base + timedelta(days=self.start_days)).isoformat(),
            end=(base + timedelta(days=self.end_days)).isoformat(),
        )

    @property
    def start(self) -> str:
        return self.resolve().start

    @property
    def end(self) -> str:
        return self.resolve().end


# Horizon: 2 weeks to 18 weeks out, about 33 Fri/Sat dates per route and ~130
# lookups per scan. That is the size CI has run reliably: 128–144 lookups per
# scan in Jul 2026, with the scan step taking 7–16 min against the job's
# 30-minute timeout. Six months would be ~48 dates per route (~192 lookups):
# half again as many Google requests from a shared runner IP, and up to ~24 min
# when Google misses and every lookup also falls back to Kayak.
SCAN_HORIZON_START_DAYS: int = 14
SCAN_HORIZON_END_DAYS: int = 126

DATE_WINDOWS: List[Union[DateWindow, RollingDateWindow]] = [
    RollingDateWindow(SCAN_HORIZON_START_DAYS, SCAN_HORIZON_END_DAYS),
]

# ---------------------------------------------------------------------------
# Operational settings
# ---------------------------------------------------------------------------

SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "120"))

# Never book more than this many new dates per route per scan run
MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN: int = 1

# Dead-man's-switch: alert if no prices have been recorded in this many hours.
# Set to 0 to disable. Default is 3× the scan interval (generous buffer).
SCANNER_WATCHDOG_HOURS: float = float(
    os.getenv("SCANNER_WATCHDOG_HOURS", str(round(SCAN_INTERVAL_MINUTES * 3 / 60, 1)))
)

# Quarantine: if more than this fraction of scanned dates trigger in one scan,
# the batch is implausible (wrong-cabin scrape) and its price rows are purged.
QUARANTINE_TRIGGER_RATE: float = float(os.getenv("QUARANTINE_TRIGGER_RATE", "0.9"))

# Keep this many eCredits in reserve (USD) — don't book if balance would drop below
MIN_ECREDIT_BUFFER_USD: float = float(os.getenv("MIN_ECREDIT_BUFFER_USD", "500.0"))

# DRY_RUN=True (default) runs the full Playwright flow but skips the final
# purchase confirmation click. Set DRY_RUN=False in env to go live.
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")

# Polite delay between outbound scrape requests (seconds)
REQUEST_DELAY_SECONDS: float = 3.0

# ---------------------------------------------------------------------------
# Credentials (never hardcode — always from env)
# ---------------------------------------------------------------------------

DELTA_USERNAME: str = os.getenv("DELTA_USERNAME", "")
DELTA_PASSWORD: str = os.getenv("DELTA_PASSWORD", "")

PUSHOVER_USER_KEY: str = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_PATH: str = os.getenv("DB_PATH", "bookings.db")
SCREENSHOT_DIR: str = os.getenv("SCREENSHOT_DIR", ".")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
